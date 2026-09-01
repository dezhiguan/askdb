export type View =
  | 'query'
  | 'tasks'
  | 'sources'
  | 'permissions'
  | 'glossary'
  | 'traces'
  | 'audit'
  | 'connectors'
  | 'developer'
  | 'roadmap'

export type ResultTab = 'result' | 'sql' | 'chain' | 'checkpoint'

export type ModalName = 'source' | 'clarification' | 'connector' | 'langfuse' | null

export interface DataSource {
  code: string
  shortName: string
  name: string
  meta: string
  status: 'online' | 'setup'
}
