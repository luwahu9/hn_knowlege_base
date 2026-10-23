// 实体一级类型集中维护；事件记录是图谱节点，不属于实体分类。
export const entityTypes = [
  {key: 'person', label: '人物', color: '--c-person'},
  {key: 'organization', label: '组织', color: '--c-org'},
  {key: 'country', label: '国家与地区', color: '--c-country'},
  {key: 'location', label: '地理位置', color: '--c-location'},
  {key: 'named_event', label: '命名事件与项目', color: '--c-named-event'},
  {key: 'legal', label: '法律与条约', color: '--c-legal'},
  {key: 'equipment', label: '装备与设施', color: '--c-equipment'},
  {key: 'honor', label: '荣誉', color: '--c-honor'},
  {key: 'group', label: '群体', color: '--c-group'},
];
export const otherEntityType = {key: 'other', label: '其他', color: '--text-3'};
export const entityType = kind => entityTypes.find(type => type.key === kind) || otherEntityType;
export const entitySubtype = subtype => subtype || '未分类';

// 与领域分类契约一致；空字符串表示人工选择“未分类”。
export const entitySubtypes = {
  person: ['政要', '政府官员', '军人', '专家学者', '外交人员', '企业家', '其他'],
  organization: ['政府部门', '监管机构', '军队', '情报机构', '政党', '智库', '高校与科研机构', '企业', '社会团体与基金会', '国际组织', '媒体', '其他'],
  country: ['国家', '行政区域'], location: ['自然区域', '地点', '边界线'],
  named_event: ['会议', '袭击', '危机', '军事行动', '项目与计划', '其他'],
  legal: ['法律', '法规', '协议', '条约'], equipment: ['设备', '武器', '通信设施', '产品'],
  honor: ['奖项', '称号'], group: ['民族', '国籍群体'],
};
