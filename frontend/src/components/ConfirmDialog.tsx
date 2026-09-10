import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

/* 全站统一的确认 / 提示弹窗。
 *
 * 存在的理由是 window.confirm / window.alert 这两样东西必须从这个产品里消失：
 * 浏览器原生弹窗顶着域名当标题（"askdb.ragforge.net 显示"），换行、强调、
 * 危险动作的红都做不了，样式还随浏览器变 —— 一个讲"可信"的控制台，
 * 在最需要人看清后果的那一刻甩出一个连自己名字都写不对的系统框，说不过去。
 *
 * 用法与 window.confirm 同形，直接 await：
 *   const { confirm } = useDialog()
 *   if (!await confirm({ title: '删除数据源「x」？', tone: 'danger' })) return
 *
 * 只做"问一句"这一件事：不放表单、不放长内容。那些是各页自己的业务弹窗
 * （AddSourceModal / TaskResultModal 一族），共用 .modal 基座，不走这里。 */

export type DialogTone = 'default' | 'danger'

export interface ConfirmOptions {
  /** 一句话把动作和对象说清楚，例如「删除数据源「pet 宠物医疗库」？」 */
  title: string
  /** 补一句后果。没有就别硬凑。 */
  description?: string
  /** 逐条列出的影响；比堆在一段里好读，也逼着写的人把后果拆开想一遍。 */
  detail?: string[]
  /** 脚注，通常用来讲**不**受影响的部分 —— 删除类操作最该说清的就是这句。 */
  note?: string
  confirmText?: string
  cancelText?: string
  /** danger 把主按钮变红，并把初始焦点放在「取消」上。 */
  tone?: DialogTone
}

export interface AlertOptions {
  title: string
  description?: string
  detail?: string[]
  note?: string
  confirmText?: string
  tone?: DialogTone
}

type Request =
  | { id: number; kind: 'confirm'; options: ConfirmOptions; settle: (ok: boolean) => void }
  | { id: number; kind: 'alert'; options: AlertOptions; settle: (ok: boolean) => void }

export interface DialogApi {
  confirm: (options: ConfirmOptions) => Promise<boolean>
  alert: (options: AlertOptions) => Promise<void>
}

const DialogContext = createContext<DialogApi | null>(null)

export function useDialog(): DialogApi {
  const api = useContext(DialogContext)
  // 兜底成 window.confirm 就等于把刚赶走的东西请回来，宁可当场炸给开发看
  if (!api) throw new Error('useDialog 必须在 <DialogProvider> 之内使用')
  return api
}

export function DialogProvider({ children }: { children: React.ReactNode }) {
  /* 队列而不是单个：两处同时问话（例如弹窗里再确认一次）时，后来的排队等，
     不会把前一个悄悄顶掉 —— 被顶掉的那个会以「取消」收场，调用方看到的是
     用户点了取消，而其实用户什么都没看见。 */
  const [queue, setQueue] = useState<Request[]>([])
  const current = queue[0] ?? null
  const seq = useRef(0)

  const api = useMemo<DialogApi>(() => ({
    confirm: options => new Promise<boolean>(resolve => {
      setQueue(list => [...list, { id: ++seq.current, kind: 'confirm', options, settle: resolve }])
    }),
    alert: options => new Promise<void>(resolve => {
      setQueue(list => [...list, { id: ++seq.current, kind: 'alert', options, settle: () => resolve() }])
    }),
  }), [])

  // 先把结果交给调用方，再把这一条从队列里摘掉：settle 不放进 setQueue 的
  // 更新函数里，那个函数 React 会重放，等于把同一个 Promise resolve 两遍
  const close = useCallback((request: Request, ok: boolean) => {
    request.settle(ok)
    setQueue(list => list.filter(item => item.id !== request.id))
  }, [])

  return (
    <DialogContext.Provider value={api}>
      {children}
      {current && (
        <DialogCard
          key={current.id}
          request={current}
          onCancel={() => close(current, false)}
          onConfirm={() => close(current, true)}
        />
      )}
    </DialogContext.Provider>
  )
}

const FOCUSABLE = 'button:not(:disabled), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'

function DialogCard({ request, onCancel, onConfirm }: {
  request: Request
  onCancel: () => void
  onConfirm: () => void
}) {
  const { options, kind } = request
  const tone = options.tone ?? (kind === 'confirm' ? 'danger' : 'default')
  const card = useRef<HTMLDivElement>(null)
  const confirmButton = useRef<HTMLButtonElement>(null)
  const cancelButton = useRef<HTMLButtonElement>(null)

  /* 开合两头的焦点。打开时按危险程度决定落点：危险动作把焦点放「取消」上，
     这样一路回车过来的人不会顺手把库删了；关闭时还回原来那个按钮，
     否则焦点掉回 body，键盘用户得从页首重新 Tab 一遍。 */
  const opener = useRef<HTMLElement | null>(null)
  useEffect(() => {
    opener.current = document.activeElement as HTMLElement | null
    const target = tone === 'danger' && kind === 'confirm' ? cancelButton.current : confirmButton.current
    target?.focus()
    return () => { opener.current?.focus?.() }
  }, [kind, tone])

  /* Esc 关闭 + Tab 圈在弹窗内。没有这道圈，Tab 会走到背后那一页的按钮上 ——
     看着是模态，键盘上却不是。 */
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); onCancel(); return }
      if (event.key !== 'Tab' || !card.current) return
      const items = Array.from(card.current.querySelectorAll<HTMLElement>(FOCUSABLE))
      if (items.length === 0) return
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement
      if (event.shiftKey && (active === first || !card.current.contains(active))) {
        event.preventDefault(); last.focus()
      } else if (!event.shiftKey && active === last) {
        event.preventDefault(); first.focus()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [onCancel])

  return (
    <div
      className="dialog-backdrop"
      /* 点遮罩空白处 = 取消，与站内其他弹窗一致；用 mousedown 而不是 click，
         免得在弹窗里按下、松手时划到遮罩上也算点了外面 */
      onMouseDown={event => { if (event.currentTarget === event.target) onCancel() }}
    >
      <div
        className={`dialog-card ${tone}`}
        ref={card}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="dialogTitle"
        aria-describedby={options.description ? 'dialogDesc' : undefined}
      >
        <div className="dialog-head">
          <i className="dialog-mark" aria-hidden="true">{tone === 'danger' ? '!' : 'i'}</i>
          <div className="dialog-copy">
            <h3 id="dialogTitle">{options.title}</h3>
            {options.description && <p id="dialogDesc">{options.description}</p>}
          </div>
        </div>
        {options.detail && options.detail.length > 0 && (
          <ul className="dialog-detail">
            {options.detail.map(line => <li key={line}>{line}</li>)}
          </ul>
        )}
        {options.note && <p className="dialog-note">{options.note}</p>}
        <div className="dialog-actions">
          {kind === 'confirm' && (
            <button className="ghost" type="button" ref={cancelButton} onClick={onCancel}>
              {(options as ConfirmOptions).cancelText ?? '取消'}
            </button>
          )}
          <button
            className={tone === 'danger' ? 'danger solid' : 'primary'}
            type="button"
            ref={confirmButton}
            onClick={onConfirm}
          >
            {options.confirmText ?? (kind === 'confirm' ? '确认' : '知道了')}
          </button>
        </div>
      </div>
    </div>
  )
}
