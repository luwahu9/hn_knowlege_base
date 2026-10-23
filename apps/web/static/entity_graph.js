import {seriesFilter} from './series.js';
import {el, api, empty, heading, docUrl} from './dom.js';
import {evidence} from './evidence.js';
import {eventControls, reviewControls, refreshReview, subtypeControl} from './review.js';
import {entityTypes, entityType, entitySubtype, otherEntityType} from './entity_types.js';

import {relationTypes, relationType, relationSummary, relationList} from './entity_relations.js';

// 类型、标签与颜色集中维护，未知类型统一回退到“其他”。
const eventType = {key: 'event', label: '事件记录', color: '--c-event'};
const types = entityTypes;
const other = otherEntityType;
const typeOf = kind => kind === 'event' ? eventType : entityType(kind);
const coordinates = new Map();
// 审核刷新重建页面时恢复当前实体；按页面地址区分全图与各局部图。
const detailSelections = new Map();
const preferences = {review: true, spin: true, mode: 1, relations: true};
const short = (value, count = 16) => value.length > count ? value.slice(0, count) + '…' : value;

export async function graphView(query) {
  const viewKey = location.hash;
  const series = query.get('series') || '';
  const seriesControl = await seriesFilter('graph', query);
  const globalUrl = '#/graph' + (series ? '?' + new URLSearchParams({series}) : '');
  const graphUrl = id => '#/graph?' + new URLSearchParams({focus: id, depth: 1, ...(series ? {series} : {})});
  let focus = query.get('focus') || '';
  const depth = query.get('depth') || '1', limit = query.get('limit') || '300';
  let entityData = null, missingEntity = false, orphan = false;
  if (focus.startsWith('ent:')) {
    try {
      entityData = await api('/api/entities/' + encodeURIComponent(focus.slice(4)));
      focus = 'ent:' + entityData.entity.id;
      if (query.get('focus') !== focus) {
        const canonical = new URLSearchParams(query); canonical.set('focus', focus);
        history.replaceState(null, '', '#/graph?' + canonical);
      }
    } catch (error) { if (error.status !== 404) throw error; missingEntity = true; }
  }
  let data = {nodes: [], edges: [], total_nodes: 0, truncated: false};
  if (!missingEntity) {
    const params = new URLSearchParams({review: preferences.review ? 1 : 0, depth, limit});
    if (focus) params.set('focus', focus);
    if (series) params.set('series', series);
    try { data = await api('/api/entity-graph?' + params); }
    catch (error) { if (error.status !== 404 || !entityData) throw error; orphan = true; }
  }
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const nodes = data.nodes.map((row, i) => {
    const cached = coordinates.get(row.id);
    const theta = i * 2.39996323, y = 1 - 2 * (i + .5) / Math.max(1, data.nodes.length);
    const radius = Math.sqrt(1 - y * y);
    return {...row, x: cached?.x ?? Math.cos(theta) * radius, y: cached?.y ?? y,
      z: cached?.z ?? Math.sin(theta) * radius, vx: 0, vy: 0, vz: 0, size: 1};
  });
  const byId = new Map(nodes.map(node => [node.id, node]));
  const edges = data.edges.map(edge => ({...edge, a: byId.get(edge.source), b: byId.get(edge.target)}));
  // 实体直连关系也参与布局和邻接判断；隐藏连线时保留节点与布局。
  for (const edge of edges.filter(item => item.kind === 'relation')) {
    const peers = edges.filter(item => item.kind === 'relation' &&
      [edge.source, edge.target].includes(item.source) && [edge.source, edge.target].includes(item.target));
    edge.bend = (peers.indexOf(edge) - (peers.length - 1) / 2) * 38 * (edge.source < edge.target ? 1 : -1);
  }
  const adjacent = new Map(nodes.map(node => [node.id, new Set([node.id])]));
  for (const edge of edges) { adjacent.get(edge.source).add(edge.target); adjacent.get(edge.target).add(edge.source); }
  let disposed = false, frame = 0, detailRevision = 0, hovered = null, selected = null, selectedEdge = null, hoveredEdge = null;
  let width = 1, height = 1, extent = 1.15, positions = [], projectedEdges = [], activeType = '', step = 0, stable = false;
  let angle = -.24, tilt = .12, zoom = 1, mode = preferences.mode, targetMode = mode, resumeAt = 0, tween = null, ripple = null;
  let drag = null, dragged = false, lastTime = 0;
  let fitPending = true, cameraX = 0, cameraY = 0;
  let palette = {}, colorMap = new Map();
  const page = el('section', {className: 'entity-graph-page'});
  const canvas = el('canvas', {className: 'entity-canvas', tabindex: 0, role: 'img',
    'aria-label': '实体与事件关系图。拖动旋转、滚轮缩放；可用搜索框或下方事件带选择节点。'});
  const context = canvas.getContext('2d');
  const detail = el('aside', {className: 'panel graph-detail'}, empty('选择一个节点', '单击节点或连线查看原文证据，双击展开一跳关系。'));
  const stage = el('div', {className: 'entity-stage'}, canvas);
  const tooltip = el('div', {className: 'graph-tooltip', hidden: true});
  const status = el('div', {className: 'graph-status', role: 'status'});
  const chip = el('div', {className: 'graph-chips'});
  if (focus) chip.append(el('a', {href: globalUrl, className: 'button'}, '← 返回全图'));
  if (data.truncated) chip.append(el('span', {className: 'truncation-note'},
    focus ? `已显示距离最近的 ${data.nodes.length} 个节点，共 ${data.total_complete === false ? '至少 ' : ''}${data.total_nodes} 个`
      : `已显示关系最多的 ${limit} 个节点，共 ${data.total_nodes} 个，可搜索或双击展开`));
  stage.append(tooltip, chip, el('div', {className: 'stage-caption'}, '拖动旋转 · 滚轮缩放 · 点击节点'), status);
  if (missingEntity || orphan || !nodes.length) stage.append(el('div', {className: 'graph-empty'},
    empty(missingEntity ? '未找到该实体' : orphan ? '该实体暂无可展示的关系' : '暂无可展示的关系'),
    el('a', {href: focus ? globalUrl : '#/timeline' + (series ? '?' + new URLSearchParams({series}) : ''), className: 'button'}, focus ? '返回全图' : '查看时间轴')));
  const input = el('input', {className: 'graph-search', type: 'search', name: 'node', placeholder: '搜索并回车定位', 'aria-label': '搜索图谱节点'});
  const modeButton = el('button', {className: 'button', type: 'button'}, targetMode ? '3D 视角' : '2D 视角');
  const spinButton = el('button', {className: 'button', type: 'button'});
  const reset = el('button', {className: 'button', type: 'button'}, '重置');
  const review = el('input', {type: 'checkbox', name: 'review', checked: preferences.review});
  const filter = el('aside', {className: 'panel graph-filters'}, el('div', {className: 'filter-heading'}, '实体类型'));
  const typeButtons = new Map();
  function typeButton(type) {
    const count = type.key ? nodes.filter(node => type.key === 'other' ? typeOf(node.kind) === other : node.kind === type.key).length : nodes.length;
    const button = el('button', {type: 'button', className: 'type-button', 'aria-pressed': String(!type.key), onclick: () => {
      setType(activeType === type.key ? '' : type.key);
    }}, el('i', {className: `legend-dot kind-${type.key || 'all'}`}), type.label, el('b', {}, count));
    button.classList.toggle('on', !type.key); typeButtons.set(type.key, button); return button;
  }
  filter.append(typeButton({key: '', label: '全部实体'}), ...types.map(typeButton));
  if (nodes.some(node => typeOf(node.kind) === other)) filter.append(typeButton(other));
  filter.append(el('hr'), typeButton(eventType));
  const relationToggle = el('input', {type: 'checkbox', name: 'relations', checked: preferences.relations});
  relationToggle.addEventListener('change', () => {
    preferences.relations = relationToggle.checked;
    if (!preferences.relations) {
      if (selectedEdge?.kind === 'relation') clear();
      if (hoveredEdge?.kind === 'relation') { hoveredEdge = null; tooltip.hidden = true; }
    }
  });
  filter.append(el('hr'), el('label', {className: 'kg-check'}, relationToggle, '显示实体关系'),
    ...relationTypes.map(type => el('div', {className: 'relation-legend'},
      el('i', {className: 'relation-legend-line relation-' + type.key}), type.label, ' →')));
  filter.append(el('hr'), el('label', {className: 'kg-check'}, review, '显示待审事件'), el('hr'),
    el('div', {className: 'filter-heading'}, '操作'), el('p', {className: 'mini'}, '单击节点或连线看详情', el('br'),
      '双击节点展开一跳', el('br'), '拖动旋转，滚轮缩放', el('br'), el('kbd', {}, 'Esc'), ' 取消选择'));
  const statistics = el('div', {className: 'graph-statistics'}, [
    ['节点', nodes.length], ['关系', edges.length], ['事件', nodes.filter(node => node.kind === 'event').length],
    ['待审关系', edges.filter(edge => edge.verdict === 'review').length],
  ].map(([label, value]) => el('div', {className: 'graph-stat'}, el('b', {}, value), el('span', {}, label))));
  const toolbar = el('div', {className: 'graph-toolbar'}, el('strong', {className: 'graph-bar-title'}, el('i'), '实体关系网络'),
    el('form', {className: 'graph-search-form', onsubmit: event => {
      event.preventDefault();
      const value = input.value.trim().toLocaleLowerCase();
      if (!value) return;
      const found = nodes.find(node => node.label.toLocaleLowerCase().includes(value) || node.aliases?.some(alias => alias.toLocaleLowerCase().includes(value)));
      if (found) { setType(''); selectNode(found, true); status.textContent = '已定位：' + found.label; }
      else status.textContent = '图中没有匹配的实体，可在审核中心或文档详情中查看';
    }}, input), modeButton, spinButton, reset);
  page.append(heading('知识图谱', '人物、组织与事件之间的关系，每条关系都能追溯到原文。', statistics), seriesControl,
    el('div', {className: 'entity-layout'}, filter, el('div', {className: 'panel graph-center'}, toolbar, stage), detail));
  const strip = el('div', {className: 'event-strip'});
  const dated = nodes.filter(node => node.kind === 'event').sort((a, b) => (a.start || '9999').localeCompare(b.start || '9999') || a.id.localeCompare(b.id));
  strip.append(...dated.map(node => el('button', {type: 'button', className: 'event-tile', 'data-node-id': node.id,
    onclick: () => selectNode(node, true)}, el('small', {}, node.start || '时间未定'), el('strong', {}, node.label), el('span', {}, node.title))));
  page.append(el('section', {className: 'panel event-band'}, el('div', {className: 'panel-heading'}, el('h2', {}, '事件时间轴'),
    el('span', {className: 'muted'}, '点击事件，图谱会转到对应节点')), strip));

  function readPalette() {
    const css = getComputedStyle(document.documentElement);
    const value = name => css.getPropertyValue(name).trim();
    palette = Object.fromEntries(['--edge', '--edge-dim', '--edge-hot', '--label', '--label-halo', '--star', '--accent', '--text-3', ...relationTypes.map(type => type.color)].map(key => [key, value(key)]));
    colorMap = new Map([...types, eventType, other].map(type => [type.key, value(type.color)]));
  }
  function paintSpin() {
    spinButton.textContent = reduced.matches ? '减少动态' : preferences.spin ? '自动旋转' : '旋转已停';
    spinButton.disabled = reduced.matches;
    spinButton.classList.toggle('on', preferences.spin && !reduced.matches);
    spinButton.setAttribute('aria-pressed', String(preferences.spin && !reduced.matches));
  }
  function setType(key) {
    if (activeType !== key) fitPending = true;
    activeType = key;
    for (const [type, button] of typeButtons) { button.classList.toggle('on', type === key); button.setAttribute('aria-pressed', String(type === key)); }
  }
  function fly(node) {
    fitPending = false;
    cameraX = cameraY = 0;
    resumeAt = performance.now() + 4500;
    const wanted = Math.atan2(node.x, node.z);
    let delta = wanted - angle; delta = Math.atan2(Math.sin(delta), Math.cos(delta));
    const projectedY = node.y;
    tween = {at: performance.now(), angle, tilt, zoom, toAngle: angle + delta, toTilt: Math.atan2(projectedY, Math.hypot(node.x, node.z)), toZoom: 1.3};
    if (reduced.matches) { angle = tween.toAngle; tilt = tween.toTilt; zoom = tween.toZoom; tween = null; }
  }
  function onDecision(decision) {
    if (decision.kind === 'event_reject' && focus === 'evt:' + decision.target) location.hash = globalUrl;
    else refreshReview();
  }
  function eventBody(node) {
    return el('div', {className: 'graph-event-evidence'}, evidence(node),
      node.problems?.length ? el('p', {className: 'kg-problems'}, '需复核：', node.problems.map(problem => ({time_missing: '缺少时间', date_unresolved: '日期无法确定', quote_ambiguous: '引文多处出现'}[problem] || problem)).join('、')) : null,
      eventControls({...node, id: node.event_id}, onDecision));
  }
  async function showEntity(node, initial = null) {
    const revision = ++detailRevision;
    detail.replaceChildren(el('div', {className: 'loading'}, '正在加载详情…'));
    try {
      const row = initial || await api('/api/entities/' + encodeURIComponent(node.entity_id));
      if (disposed || detailRevision !== revision) return;
      const entity = row.entity;
      detailSelections.set(viewKey, entity.id);
      const linked = [...(adjacent.get('ent:' + entity.id) || [])].filter(id => id !== 'ent:' + entity.id).map(id => byId.get(id)).filter(Boolean);
      const article = el('article', {className: 'knowledge-detail'}, el('span', {className: 'badge kind-' + entityType(entity.type).key}, '● ' + entityType(entity.type).label), el('h2', {}, entity.canonical),
        el('p', {className: 'entity-subtype'}, '子类：', entitySubtype(entity.subtype)), subtypeControl(entity),
        row.redirected_from ? el('p', {className: 'muted entity-redirect'}, '已合并至此实体（原 ID：', row.redirected_from, '）') : null,
        el('p', {className: 'muted'}, entity.aliases.length ? '别名：' + entity.aliases.join('、') : '暂无别名'),
        el('div', {className: 'entity-metrics'}, el('div', {}, el('small', {}, '提及次数'), el('strong', {}, `${row.mentions} 次`)),
          el('div', {}, el('small', {}, '来源文档'), el('strong', {}, `${row.documents.length} 份`))),
        relationList(row, graphUrl), el('h3', {}, '关联对象'), linked.length ? el('div', {className: 'related-nodes'}, linked.map(item => el('button',
          {type: 'button', className: 'related-node', onclick: () => selectNode(item, true)}, el('small', {}, typeOf(item.kind).label), item.label))) : el('p', {className: 'muted'}, '暂无当前图中的关联对象'),
        el('h3', {}, '出现的文档'), el('div', {className: 'kg-link-list'}, row.documents.map(doc => el('a', {href: docUrl(doc.version_id)}, doc.title, `（${doc.count} 次）`))),
        row.documents_truncated ? el('p', {className: 'truncation-note'}, '仅显示前 500 个') : null,
        el('h3', {}, '相关事件'), row.events.length ? el('div', {className: 'kg-link-list'}, row.events.map(event => el('button', {type: 'button', className: 'kg-text-button', onclick: () => {
          const current = byId.get('evt:' + event.id);
          if (current) selectNode(current, true);
          else { detailRevision++; detail.replaceChildren(el('article', {className: 'knowledge-detail'}, el('h2', {}, event.label), eventBody({...event, event_id: event.id}))); }
        }}, event.label))) : el('p', {className: 'muted'}, '暂无相关事件'),
        row.events_truncated ? el('p', {className: 'truncation-note'}, '仅显示最近 500 条') : null);
      if (row.candidates.length) article.append(el('h3', {}, '可能是同一实体'), ...row.candidates.map(candidate =>
        el('div', {className: 'entity-candidate'}, el('a', {href: graphUrl('ent:' + candidate.id)}, candidate.canonical),
          el('p', {className: 'muted'}, entityType(candidate.type).label), reviewControls([{text: '合并到此', kind: 'entity_merge', target: entity.id, value: candidate.id}]))),
        reviewControls([{text: '不是同一实体', kind: 'entity_distinct', target: entity.id}]));
      const proofs = row.events.slice(0, 2);
      if (proofs.length) article.append(el('h3', {}, '关系证据'), ...proofs.map(event => eventBody({...event, event_id: event.id})));
      detail.replaceChildren(article);
    } catch (error) { if (!disposed && revision === detailRevision) detail.replaceChildren(empty('详情加载失败', error.message)); }
  }
  function selectNode(node, locate = false) {
    if (!allowed(node)) setType('');
    if (node.kind === 'event') detailSelections.delete(viewKey);
    selected = node; selectedEdge = null;
    ripple = reduced.matches ? null : {node, at: performance.now()};
    if (locate) fly(node);
    strip.querySelectorAll('.event-tile').forEach(button => button.classList.toggle('selected', button.dataset.nodeId === node.id));
    if (node.kind === 'event') { detailRevision++; detail.replaceChildren(el('article', {className: 'knowledge-detail'}, el('span', {className: 'badge kg-' + node.verdict}, node.verdict === 'review' ? '待审事件' : '事件'), el('h2', {}, node.label), eventBody(node))); }
    else showEntity(node);
  }
  function selectEdge(edge) {
    detailSelections.delete(viewKey);
    selected = null; selectedEdge = edge; detailRevision++;
    if (edge.kind === 'relation') {
      detail.replaceChildren(el('article', {className: 'knowledge-detail'}, el('h2', {}, edge.label),
        el('p', {}, relationSummary(edge)),
        el('div', {className: 'edge-endpoints'},
          el('button', {type: 'button', className: 'kg-text-button', onclick: () => selectNode(edge.a, true)}, edge.a.label),
          el('span', {}, '↓ ' + edge.label),
          el('button', {type: 'button', className: 'kg-text-button', onclick: () => selectNode(edge.b, true)}, edge.b.label)),
        el('p', {className: 'muted'}, '点击实体查看逐条关系、时间和原文证据。')));
      return;
    }
    const event = byId.get('evt:' + edge.event_id);
    detail.replaceChildren(el('article', {className: 'knowledge-detail'}, el('span', {className: 'badge'}, '关系'), el('h2', {}, edge.label),
      el('div', {className: 'edge-endpoints'}, el('button', {type: 'button', className: 'kg-text-button', onclick: () => selectNode(edge.a, true)}, edge.a.label), el('span', {}, '↓ ' + edge.label),
        el('button', {type: 'button', className: 'kg-text-button', onclick: () => selectNode(edge.b, true)}, edge.b.label)), eventBody(event)));
  }
  function clear() { detailSelections.delete(viewKey); selected = selectedEdge = null; detailRevision++; detail.replaceChildren(empty('选择一个节点', '单击节点或连线查看原文证据，双击展开一跳关系。')); strip.querySelectorAll('.selected').forEach(button => button.classList.remove('selected')); }

  // 力导向只改变三维坐标；投影与镜头状态独立，重新进入页面复用坐标。
  function simulate() {
    if (stable || !nodes.length) return;
    const samples = nodes.length > 400 ? Math.ceil(nodes.length / 160) : 1;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j += samples) {
        const b = nodes[j], dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
        const d2 = dx * dx + dy * dy + dz * dz + .035;
        const force = .0035 * samples / (d2 * Math.sqrt(d2) * Math.sqrt(nodes.length));
        a.vx += dx * force; a.vy += dy * force; a.vz += dz * force;
        b.vx -= dx * force; b.vy -= dy * force; b.vz -= dz * force;
      }
    }
    for (const {a, b} of edges) {
      const dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z, distance = Math.hypot(dx, dy, dz) || .01;
      const force = (distance - .5) / distance * .018;
      a.vx += dx * force; a.vy += dy * force; a.vz += dz * force;
      b.vx -= dx * force; b.vy -= dy * force; b.vz -= dz * force;
    }
    let energy = 0;
    for (const n of nodes) {
      n.vx = (n.vx - n.x * .002) * .72; n.vy = (n.vy - n.y * .002) * .72; n.vz = (n.vz - n.z * .002) * .72;
      n.x += n.vx; n.y += n.vy; n.z += n.vz;
      energy += n.vx * n.vx + n.vy * n.vy + n.vz * n.vz;
    }
    step++; stable = step > 550 || (step > 100 && energy < nodes.length * .000001);
  }
  function project(node, time) {
    const drift = stable && !reduced.matches ? Math.sin(time * .0003 + nodes.indexOf(node)) * .008 : 0;
    const x = node.x + drift, y = node.y, z = node.z;
    const u = x * Math.cos(angle) - z * Math.sin(angle), v = x * Math.sin(angle) + z * Math.cos(angle);
    const yy = y * Math.cos(tilt) - v * Math.sin(tilt), zz = y * Math.sin(tilt) + v * Math.cos(tilt);
    const px = x + (u - x) * mode, py = y + (yy - y) * mode, pz = zz * mode;
    const scale = 2.85 / Math.max(.7, 2.85 - pz * .31), unit = Math.min(width * .29, height * .35) * zoom * 1.15 / extent;
    return {node, x: width * .5 + cameraX + px * unit * scale, y: height * .51 + cameraY + py * unit * .79 * scale, z: pz, scale};
  }
  function fitCamera(time) {
    const visible = nodes.filter(allowed);
    if (!visible.length || width <= 1 || height <= 1) return;
    zoom = 1; cameraX = cameraY = 0;
    const points = visible.map(node => project(node, time));
    const left = Math.min(...points.map(p => p.x)), right = Math.max(...points.map(p => p.x));
    const top = Math.min(...points.map(p => p.y)), bottom = Math.max(...points.map(p => p.y));
    const padX = Math.max(28, width * .1), padY = Math.max(32, height * .1);
    zoom = Math.min((width - 2 * padX) / Math.max(40, right - left), (height - 2 * padY) / Math.max(40, bottom - top));
    cameraX = -((left + right) / 2 - width * .5) * zoom;
    cameraY = height * .5 - height * .51 - ((top + bottom) / 2 - height * .51) * zoom;
    // 自动旋转时跟随可见范围；布局和旋转均停下后固定镜头。
    // 用户开始缩放、旋转或定位时立即退出自动取景。
    if (stable && (!preferences.spin || reduced.matches)) fitPending = false;
  }
  function allowed(node) {
    if (!activeType) return true;
    return [...adjacent.get(node.id)].some(id => activeType === 'other' ? typeOf(byId.get(id).kind) === other : byId.get(id).kind === activeType);
  }
  function glow(x, y, r, color, alpha = 1) {
    context.globalAlpha = alpha;
    const gradient = context.createRadialGradient(x, y, 0, x, y, r * 3.8);
    gradient.addColorStop(0, color + '77'); gradient.addColorStop(.38, color + '29'); gradient.addColorStop(1, color + '00');
    context.fillStyle = gradient; context.beginPath(); context.arc(x, y, r * 3.8, 0, Math.PI * 2); context.fill();
    context.fillStyle = color; context.beginPath(); context.arc(x, y, r, 0, Math.PI * 2); context.fill();
    context.globalAlpha = 1;
  }
  function label(text, x, y, color, align = 'center') {
    context.font = '13px "Microsoft YaHei", sans-serif'; context.textAlign = align;
    context.lineWidth = 4; context.strokeStyle = palette['--label-halo']; context.strokeText(text, x, y);
    context.fillStyle = color; context.fillText(text, x, y);
  }
  function edgePoint(a, b, bend, t) {
    const dx = b.x - a.x, dy = b.y - a.y, length = Math.hypot(dx, dy) || 1;
    const cx = (a.x + b.x) / 2 - dy / length * bend, cy = (a.y + b.y) / 2 + dx / length * bend;
    return {x: (1 - t) ** 2 * a.x + 2 * (1 - t) * t * cx + t * t * b.x,
      y: (1 - t) ** 2 * a.y + 2 * (1 - t) * t * cy + t * t * b.y, cx, cy};
  }
  function draw(time) {
    if (disposed) return;
    const dt = Math.min(40, time - (lastTime || time)); lastTime = time;
    for (let i = 0; i < (reduced.matches ? 12 : 3); i++) simulate();
    if (tween) {
      const fraction = Math.min(1, (time - tween.at) / 700), eased = 1 - (1 - fraction) ** 3;
      angle = tween.angle + (tween.toAngle - tween.angle) * eased;
      tilt = tween.tilt + (tween.toTilt - tween.tilt) * eased;
      zoom = tween.zoom + (tween.toZoom - tween.zoom) * eased;
      if (fraction === 1) tween = null;
    } else if (preferences.spin && !reduced.matches && !drag && time > resumeAt) angle += dt * .000085 * mode;
    mode = reduced.matches ? targetMode : mode + (targetMode - mode) * Math.min(1, dt * .008);
    const dpr = Math.min(2, devicePixelRatio || 1);
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) { canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr); }
    context.setTransform(dpr, 0, 0, dpr, 0, 0); context.clearRect(0, 0, width, height);
    for (let i = 0; i < 95; i++) { context.fillStyle = `rgba(${palette['--star']},${.08 + (i % 5) * .035})`; context.fillRect(((i * 187.3) % 997) / 997 * width, ((i * 97.7) % 613) / 613 * height, 1, 1); }
    extent = Math.max(1.15, ...nodes.map(node => Math.hypot(node.x, node.y, node.z)));
    if (fitPending) fitCamera(time);
    positions = nodes.filter(allowed).map(node => project(node, time)).sort((a, b) => a.z - b.z);
    const projected = new Map(positions.map(point => [point.node.id, point]));
    const chosen = selected || hovered;
    const highlight = chosen ? adjacent.get(chosen.id) : selectedEdge || hoveredEdge ? new Set([(selectedEdge || hoveredEdge).source, (selectedEdge || hoveredEdge).target]) : null;
    projectedEdges = [];
    for (const edge of edges) {
      if (edge.kind === 'relation' && !preferences.relations) continue;
      const a = projected.get(edge.source), b = projected.get(edge.target); if (!a || !b) continue;
      const hot = Boolean(chosen && [edge.source, edge.target].includes(chosen.id)) || selectedEdge?.id === edge.id || hoveredEdge?.id === edge.id;
      const dim = highlight && !hot;
      projectedEdges.push({edge, a, b});
      const relation = edge.kind === 'relation' ? relationType(edge.relation) : null;
      const color = relation ? palette[relation.color] : palette[hot ? '--edge-hot' : dim ? '--edge-dim' : '--edge'];
      const middle = edgePoint(a, b, edge.bend || 0, .5);
      context.beginPath(); context.moveTo(a.x, a.y);
      if (relation) context.quadraticCurveTo(middle.cx, middle.cy, b.x, b.y);
      else context.lineTo(b.x, b.y);
      context.strokeStyle = color; context.lineWidth = relation ? (hot ? 3 : 1.8) : hot ? 2 : .85;
      context.globalAlpha = relation && dim ? .25 : 1;
      context.setLineDash(relation ? relation.dash : edge.verdict === 'review' ? [5, 5] : []);
      context.stroke(); context.setLineDash([]);
      if (hot || relation) {
        const tail = edgePoint(a, b, edge.bend || 0, .9);
        const theta = Math.atan2(b.y - tail.y, b.x - tail.x), tx = b.x - Math.cos(theta) * 14, ty = b.y - Math.sin(theta) * 14;
        context.beginPath(); context.moveTo(tx, ty); context.lineTo(tx - Math.cos(theta - .45) * 8, ty - Math.sin(theta - .45) * 8);
        context.lineTo(tx - Math.cos(theta + .45) * 8, ty - Math.sin(theta + .45) * 8); context.closePath(); context.fillStyle = color; context.fill();
        if (hot) label(edge.label, middle.x, middle.y - 8, palette['--label']);
      }
      context.globalAlpha = 1;
      if (!relation && !reduced.matches && !dim && (nodes.length <= 400 || hot)) for (let k = 0; k < (hot ? 2 : 1); k++) {
        const fraction = (time * (hot ? .00028 : .00014) + edges.indexOf(edge) * .17 + k / 2) % 1;
        glow(a.x + (b.x - a.x) * fraction, a.y + (b.y - a.y) * fraction, hot ? 2 : 1, colorMap.get('event'), hot ? .9 : .35);
      }
    }
    for (const p of positions) {
      const n = p.node, active = selected?.id === n.id || hovered?.id === n.id;
      const targetSize = active ? 1.4 : 1;
      n.size = reduced.matches ? targetSize : n.size + (targetSize - n.size) * .18;
      const breath = reduced.matches || nodes.length > 400 ? 1 : 1 + .07 * Math.sin(time * .002 + nodes.indexOf(n));
      const radius = Math.min(15, 5.5 + Math.sqrt(n.degree) * 1.7) * p.scale * n.size * breath;
      p.radius = radius; p.alpha = highlight && !highlight.has(n.id) ? .2 : 1;
      const alpha = highlight && !highlight.has(n.id) ? .2 : 1;
      const color = colorMap.get(typeOf(n.kind).key);
      glow(p.x, p.y, radius, color, alpha);
      if (active) { context.strokeStyle = color; context.lineWidth = 1; context.beginPath(); context.arc(p.x, p.y, radius + 6, 0, Math.PI * 2); context.stroke(); }
      if (ripple?.node.id === n.id && !reduced.matches) {
        const t = (time - ripple.at) / 650;
        if (t < 1) { context.globalAlpha = 1 - t; context.strokeStyle = color; context.beginPath(); context.arc(p.x, p.y, radius + t * 34, 0, Math.PI * 2); context.stroke(); context.globalAlpha = 1; } else ripple = null;
      }
    }
    const labelBoxes = [];
    // 近处节点先分配文字区域，后来的远处标签遇到重叠即让位。
    for (const p of [...positions].sort((a, b) => b.z - a.z)) {
      const n = p.node, active = selected?.id === n.id || hovered?.id === n.id;
      if (n.kind === 'event' && !active && positions.length > 20 && !adjacent.get(selected?.id)?.has(n.id)) continue;
      const text = short(n.label, active ? 24 : 13);
      context.font = '13px "Microsoft YaHei", sans-serif';
      const textWidth = context.measureText(text).width;
      const labelX = Math.max(textWidth / 2 + 6, Math.min(width - textWidth / 2 - 6, p.x));
      const labelY = Math.min(height - 28, p.y + p.radius + 17);
      const box = {left: labelX - textWidth / 2, right: labelX + textWidth / 2, top: labelY - 13, bottom: labelY + 3};
      const overlaps = labelBoxes.some(other => box.left < other.right + 4 && box.right > other.left - 4 && box.top < other.bottom + 2 && box.bottom > other.top - 2);
      if (!overlaps) {
        context.globalAlpha = p.alpha; label(text, labelX, labelY, palette['--label']); context.globalAlpha = 1;
        labelBoxes.push(box);
      }
    }
    frame = requestAnimationFrame(draw);
  }
  function hit(event) {
    const rect = canvas.getBoundingClientRect(), x = event.clientX - rect.left, y = event.clientY - rect.top;
    const node = [...positions].sort((a, b) => Math.hypot(a.x - x, a.y - y) - Math.hypot(b.x - x, b.y - y)).find(p => Math.hypot(p.x - x, p.y - y) <= 20);
    if (node) return {node: node.node, x, y};
    const edge = projectedEdges.find(({edge, a, b}) => {
      if (edge.kind === 'relation') {
        let previous = edgePoint(a, b, edge.bend || 0, .08);
        for (let i = 1; i <= 42; i++) {
          const point = edgePoint(a, b, edge.bend || 0, .08 + i * .02);
          const dx = point.x - previous.x, dy = point.y - previous.y;
          const t = Math.max(0, Math.min(1, ((x - previous.x) * dx + (y - previous.y) * dy) / (dx * dx + dy * dy || 1)));
          if (Math.hypot(x - previous.x - t * dx, y - previous.y - t * dy) < 6) return true;
          previous = point;
        }
        return false;
      }
      const dx = b.x - a.x, dy = b.y - a.y, t = ((x - a.x) * dx + (y - a.y) * dy) / (dx * dx + dy * dy);
      return t > .08 && t < .92 && Math.hypot(x - (a.x + t * dx), y - (a.y + t * dy)) < 6;
    });
    return {edge: edge?.edge, x, y};
  }
  canvas.addEventListener('pointerdown', event => { fitPending = false; canvas.focus({preventScroll: true}); canvas.setPointerCapture(event.pointerId); drag = {x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY}; dragged = false; tween = null; });
  canvas.addEventListener('pointermove', event => {
    if (drag) { const dx = event.clientX - drag.x, dy = event.clientY - drag.y; if (Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) > 3) dragged = true;
      angle += dx * .006; tilt = Math.max(-.85, Math.min(.85, tilt + dy * .004)); drag.x = event.clientX; drag.y = event.clientY; tooltip.hidden = true; return; }
    const target = hit(event); hovered = target.node || null; hoveredEdge = target.edge || null;
    tooltip.hidden = !hovered && !hoveredEdge;
    if (!tooltip.hidden) { tooltip.textContent = hovered ? `${typeOf(hovered.kind).label}${hovered.kind === 'event' ? '' : ' / ' + entitySubtype(hovered.subtype)} · ${hovered.label}` : `${hoveredEdge.a.label} → ${hoveredEdge.label} → ${hoveredEdge.b.label}${hoveredEdge.kind === 'relation' ? ' · ' + relationSummary(hoveredEdge) : ''}`;
      tooltip.style.left = Math.max(8, Math.min(width - 240, target.x + 14)) + 'px'; tooltip.style.top = Math.max(8, target.y - 38) + 'px'; }
    canvas.style.cursor = hovered || hoveredEdge ? 'pointer' : 'grab';
  });
  canvas.addEventListener('pointerup', event => { drag = null; resumeAt = performance.now() + 1000; if (dragged) return; const target = hit(event);
    if (target.node) selectNode(target.node); else if (target.edge) selectEdge(target.edge); else clear(); });
  canvas.addEventListener('pointercancel', () => { drag = null; });
  canvas.addEventListener('pointerleave', () => { hovered = hoveredEdge = null; tooltip.hidden = true; });
  canvas.addEventListener('dblclick', event => { const target = hit(event); if (target.node) location.hash = graphUrl(target.node.id); });
  canvas.addEventListener('wheel', event => { event.preventDefault(); fitPending = false; zoom = Math.max(.15, Math.min(8, zoom * (event.deltaY > 0 ? .9 : 1.1))); tween = null; }, {passive: false});
  const escape = event => { if (event.key === 'Escape') clear(); };
  modeButton.addEventListener('click', () => { targetMode = targetMode ? 0 : 1; preferences.mode = targetMode; modeButton.textContent = targetMode ? '3D 视角' : '2D 视角'; modeButton.classList.toggle('on', Boolean(targetMode)); });
  spinButton.addEventListener('click', () => { preferences.spin = !preferences.spin; paintSpin(); });
  reset.addEventListener('click', () => {
    angle = -.24; tilt = .12; zoom = 1; cameraX = cameraY = 0; fitPending = true; tween = null; resumeAt = 0; input.value = '';
    targetMode = preferences.mode = 1; preferences.spin = true; modeButton.textContent = '3D 视角'; modeButton.classList.add('on'); paintSpin();
    setType(''); clear(); status.textContent = '';
    if (focus) location.hash = globalUrl;
  });
  review.addEventListener('change', () => {
    preferences.review = review.checked;
    if (!review.checked && byId.get(focus)?.verdict === 'review') location.hash = globalUrl;
    else refreshReview();
  });
  const resize = new ResizeObserver(() => { width = stage.clientWidth; height = stage.clientHeight; });
  page.mount = () => { readPalette(); paintSpin(); modeButton.classList.toggle('on', Boolean(targetMode)); resize.observe(stage);
    window.addEventListener('kb:theme', readPalette); window.addEventListener('keydown', escape); reduced.addEventListener('change', paintSpin);
    width = stage.clientWidth; height = stage.clientHeight; frame = requestAnimationFrame(draw);
    const restored = byId.get('ent:' + detailSelections.get(viewKey));
    if (restored) selectNode(restored);
    else if (entityData) showEntity({entity_id: entityData.entity.id}, entityData);
    else if (byId.has(focus)) selectNode(byId.get(focus));
  };
  page.dispose = () => { disposed = true; detailRevision++; cancelAnimationFrame(frame); resize.disconnect();
    window.removeEventListener('kb:theme', readPalette); window.removeEventListener('keydown', escape); reduced.removeEventListener('change', paintSpin);
    for (const node of nodes) coordinates.set(node.id, {x: node.x, y: node.y, z: node.z});
  };
  return page;
}
