# 本地浏览器依赖

仅通过同源静态文件加载，不使用 CDN、不引入构建工具、不修改 CSP。

| 名称 | 版本 | 本地文件 / 发布包中的路径 | 官方来源 | 许可证 |
|---|---|---|---|---|
| Cytoscape.js | 3.34.3 | `cytoscape.min.js` / `dist/cytoscape.min.js` | https://github.com/cytoscape/cytoscape.js ; https://registry.npmjs.org/cytoscape/3.34.3 | MIT |
| Moment | 2.31.0 | `moment.min.js` / `min/moment.min.js` | https://github.com/moment/moment ; https://registry.npmjs.org/moment/2.31.0 | MIT |
| vis-data | 8.0.5 | `vis-data.min.js` / `peer/umd/vis-data.min.js` | https://github.com/visjs/vis-data ; https://registry.npmjs.org/vis-data/8.0.5 | Apache-2.0 OR MIT |
| vis-timeline | 8.5.4 | `vis-timeline.min.js` / `peer/umd/vis-timeline-graph2d.min.js`；`vis-timeline.min.css` / `styles/vis-timeline-graph2d.min.css` | https://github.com/visjs/vis-timeline ; https://registry.npmjs.org/vis-timeline/8.5.4 | Apache-2.0 OR MIT |

Cytoscape.js 3.34.3 的 npm 发布日期为 2026-09-07，选用时（2026-10-19）已发布超过两周。
上述文件从对应 npm 官方发布包直接提取，提取前按发布元数据中的 integrity 校验，未改动库内容。

加载顺序为 Moment → vis-data → vis-timeline，使用经典 UMD 脚本及独立 CSS。Moment 只供 vis-timeline 使用，应用代码不调用 Moment；应用日期运算使用 ISO 字符串和原生 Date。
时间轴的条目与分组内容传入文本构成的 DOM 元素；图谱标签为画布文字，均不将服务端文本作为 HTML。
