/** 环境档位 —— 前端这一侧的唯一出处。
 *
 *  在此之前它散在三个地方：AddSourceModal 里两行写死的 <option>、
 *  DataSourcesPage 的 ENV_LABEL、TrustSidebar 的 dbRoleLabel。三处各写各的，
 *  于是同一个源在下拉里叫「生产只读镜像」、在卡片上叫「生产只读」、
 *  在右栏又变成 PROD-RO。收到一处，加一档只改这里。
 *
 *  顺序与后端 sources.ENVS 一致，**按离生产的距离从远到近** ——
 *  下拉按这个顺序渲染，把生产排在中间会让点错的概率高一档。
 *
 *  生产那一档的值是 `prod_ro` 而不是 `prod`：注册表里已有的生产源存的就是
 *  这个值，改字面量等于让存量源变成一个界面不认识的档位。显示名可以改，
 *  存储值不能。
 */
export const ENV_ORDER = ['dev', 'test', 'staging', 'prod_ro'] as const

export type EnvCode = (typeof ENV_ORDER)[number]

/** 界面上给人看的写法。 */
export const ENV_LABEL: Record<string, string> = {
  dev: '开发环境',
  test: '测试环境',
  staging: '预生产环境',
  prod_ro: '生产环境',
  builtin: '内置',
}

/** 角标位那种放不下全名的地方用的短码。与后端 sources.ENV_LABEL 对齐。 */
export const ENV_SHORT: Record<string, string> = {
  dev: 'DEV',
  test: 'TEST',
  staging: 'STAGING',
  prod_ro: 'PROD',
}

/** 认不出来的档位如实回显原值 —— 拿一个默认档去顶替，等于把"这个源属于哪
 *  一档"这件事悄悄答错，而这一栏存在的意义就是回答它。 */
export const envLabel = (env?: string): string => ENV_LABEL[env ?? ''] ?? (env || '—')
export const envShort = (env?: string): string => ENV_SHORT[env ?? ''] ?? (env || '').toUpperCase()
