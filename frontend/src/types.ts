export type View =
  | 'query'
  | 'tasks'
  | 'sources'
  | 'permissions'
  | 'glossary'
  | 'evaluation'
  | 'traces'
  | 'audit'

export type ResultTab = 'result' | 'sql' | 'chain' | 'checkpoint'

// 'source'（添加数据源）已随数据源页接入真实后端一并移除：
// askdb 的连接由配置文件指定，页面上不存在「新增数据源」这件事，
// 更不该留一个收数据库口令的表单在包里等人接回去。
export type ModalName = 'clarification' | 'langfuse' | null

