import { PageHeader } from '../components/AppShell'
import type { ModalName } from '../types'

export function DataSourcesPage({ openModal, notify }: { openModal: (modal: ModalName) => void; notify: (message: string) => void }) {
  const cards = [
    ['PG', '订单中心只读镜像', 'PostgreSQL 15 · prod-replica.internal:5432', '18ms', '42', 'VAULT', '正常'],
    ['CK', '用户行为分析', 'ClickHouse · analytics.internal:9440', '24ms', '18', 'VAULT', '正常'],
    ['MY', '仓储测试库', 'MySQL 8 · staging-wms.internal:3306', '—', '0', 'MISSING', '待配置'],
  ]
  return <div className="page"><PageHeader eyebrow="Phase 1 · Readonly Data" title="数据源管理" description="只连接测试库和生产只读镜像，凭证由平台密钥库托管。" action={<button className="primary" onClick={() => openModal('source')}>＋ 添加数据源</button>} />
    <div className="source-grid">{cards.map(card => <article className="source-card" key={card[0]}><div className="source-top"><i>{card[0]}</i><span className={`status ${card[6] === '正常' ? '' : 'wait'}`}>● {card[6]}</span></div><h3>{card[1]}</h3><p>{card[2]}</p><div className="mini-metrics"><div><span>延迟</span><strong>{card[3]}</strong></div><div><span>可见表</span><strong>{card[4]}</strong></div><div><span>凭证</span><strong>{card[5]}</strong></div><div><span>最后检查</span><strong>{card[6] === '正常' ? '1m ago' : 'NEVER'}</strong></div></div><div className="card-actions"><button className={card[6] === '正常' ? 'secondary' : 'primary'} onClick={() => card[6] === '正常' ? notify('连接成功 · 只读账号和网络策略检查通过') : openModal('source')}>{card[6] === '正常' ? '测试连接' : '完成配置'}</button><button className="ghost">配置</button></div></article>)}</div>
  </div>
}
