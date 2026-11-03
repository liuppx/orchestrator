import type { ReactNode } from 'react'

export function PageHeader({
  title,
  description,
  breadcrumb,
  actions,
}: {
  title: string
  description?: string
  breadcrumb?: ReactNode
  actions?: ReactNode
}) {
  return (
    <header className="app-page-header">
      {breadcrumb ? <div className="app-page-breadcrumb">{breadcrumb}</div> : null}
      <div className="app-page-heading-row">
        <div className="app-page-heading-copy">
          <h1>{title}</h1>
          {description ? <p>{description}</p> : null}
        </div>
        {actions ? <div className="app-page-actions">{actions}</div> : null}
      </div>
    </header>
  )
}
