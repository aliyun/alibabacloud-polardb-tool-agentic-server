import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'

import type { ConfigModule } from '../../api/configuration'
import ConfigModuleForm from './index'

const module: ConfigModule = {
  name: 'user_sso',
  revision: 2,
  workflow_state: 'DRAFT',
  draft: {
    client_id: 'client',
    client_secret: { configured: true },
  },
  effective: null,
  dependencies: ['token_security'],
  dependents: [],
  ui_hints: { secret_fields: ['client_secret'] },
  schema: {
    type: 'object',
    required: ['client_id', 'client_secret'],
    properties: {
      client_id: { type: 'string', title: 'Client ID' },
      client_secret: { type: 'string', title: 'Client secret' },
      idp_pkce: { type: 'boolean', title: 'Use PKCE' },
      scopes: { type: 'array', title: 'Scopes', items: { type: 'string' } },
    },
  },
}

it('renders schema fields and preserves configured secret markers', () => {
  render(<ConfigModuleForm module={module} onSubmit={vi.fn()} />)

  expect(screen.getByLabelText('Client ID')).toHaveValue('client')
  expect(screen.getByLabelText(/Client secret/i)).toHaveAttribute(
    'placeholder',
    'Configured — leave blank to keep',
  )
  expect(screen.getByText(/value is never returned/i)).toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: /use pkce/i })).toBeInTheDocument()
})

it('submits changed values from a schema-driven form', async () => {
  const user = userEvent.setup()
  const onSubmit = vi.fn()
  render(
    <>
      <ConfigModuleForm module={module} onSubmit={onSubmit} formId="module-form" />
      <button type="submit" form="module-form">
        Continue
      </button>
    </>,
  )

  await user.clear(screen.getByLabelText('Client ID'))
  await user.type(screen.getByLabelText('Client ID'), 'updated-client')
  await user.click(screen.getByRole('button', { name: 'Continue' }))

  expect(onSubmit).toHaveBeenCalledWith(
    expect.objectContaining({ client_id: 'updated-client' }),
  )
})

it('enforces schema string lengths and reports value changes', async () => {
  const user = userEvent.setup()
  const onSubmit = vi.fn()
  const onValuesChange = vi.fn()
  const coreAdmin: ConfigModule = {
    name: 'core_admin',
    revision: 0,
    workflow_state: 'NOT_CONFIGURED',
    draft: null,
    effective: null,
    dependencies: ['token_security'],
    dependents: [],
    ui_hints: { secret_fields: ['password'] },
    schema: {
      type: 'object',
      required: ['password'],
      properties: {
        password: {
          type: 'string',
          title: 'Administrator password',
          minLength: 12,
        },
      },
    },
  }
  render(
    <>
      <ConfigModuleForm
        module={coreAdmin}
        onSubmit={onSubmit}
        onValuesChange={onValuesChange}
        formId="core-admin-form"
      />
      <button type="submit" form="core-admin-form">
        Submit
      </button>
    </>,
  )

  await user.type(
    screen.getByLabelText(/administrator password/i),
    'short',
  )
  await user.click(screen.getByRole('button', { name: /submit/i }))

  expect(
    await screen.findByText(/must be at least 12 characters/i),
  ).toBeInTheDocument()
  expect(onSubmit).not.toHaveBeenCalled()
  expect(onValuesChange).toHaveBeenCalled()
})

it('renders module documentation links with safe external attributes', () => {
  const withDocs: ConfigModule = {
    ...module,
    ui_hints: {
      docs: [
        {
          label: 'RAM AccessKey guide',
          url: 'https://help.aliyun.com/zh/ram/example',
          description: 'Grant PolarDB creation permission.',
        },
      ],
    },
  }
  render(<ConfigModuleForm module={withDocs} onSubmit={vi.fn()} />)

  const link = screen.getByRole('link', { name: 'RAM AccessKey guide' })
  expect(link).toHaveAttribute('href', 'https://help.aliyun.com/zh/ram/example')
  expect(link).toHaveAttribute('target', '_blank')
  expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'))
  expect(screen.getByText(/Grant PolarDB creation permission/)).toBeInTheDocument()
})

it('omits the documentation block when no docs are provided', () => {
  render(<ConfigModuleForm module={module} onSubmit={vi.fn()} />)

  expect(screen.queryByRole('link')).not.toBeInTheDocument()
})
