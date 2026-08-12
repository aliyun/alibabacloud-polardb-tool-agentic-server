import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'

import type { ConfigModule } from '../../api/configuration'
import AliyunAccessForm from './index'
import { i18n } from '../../i18n/i18n'

const directModule: ConfigModule = {
  name: 'aliyun_access',
  revision: 2,
  workflow_state: 'ACTIVE',
  draft: {
    credential_mode: 'direct_ak',
    region_id: 'cn-hangzhou',
    openapi_network: 'public',
    direct_ak: {
      access_key_id: { configured: true, display_hint: 'LTAI****ABCD', updated_at: '2026-08-09T00:00:00Z' },
      access_key_secret: { configured: true, updated_at: '2026-08-09T00:00:00Z' },
    },
  },
  effective: null,
  schema: { type: 'object' },
  dependencies: [],
  dependents: [],
}

function renderForm(module = directModule, onSubmit = vi.fn()) {
  render(
    <>
      <AliyunAccessForm module={module} onSubmit={onSubmit} formId="aliyun-access-form" />
      <button type="submit" form="aliyun-access-form">Submit</button>
    </>,
  )
  return onSubmit
}

function transitionAlertFor(message: RegExp): HTMLElement {
  const alert = screen.getByText(message).closest('.aliyun-access-form__transition')
  if (!(alert instanceof HTMLElement)) {
    throw new Error('Expected a credential transition alert')
  }
  return alert
}

it('shows the selected mode fields while rendering stored credentials as markers', async () => {
  const user = userEvent.setup()
  renderForm()

  expect(screen.getByText('LTAI****ABCD')).toBeInTheDocument()
  expect(screen.queryByRole('textbox', { name: /accesskey id/i })).not.toBeInTheDocument()
  expect(screen.queryByLabelText(/accesskey secret/i)).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: /replace accesskey/i }))
  expect(screen.getByLabelText(/accesskey id/i)).toHaveValue('')
  expect(screen.getByLabelText(/accesskey secret/i)).toHaveValue('')
  expect(screen.queryByLabelText(/role arn/i)).not.toBeInTheDocument()

  await user.click(screen.getByRole('radio', { name: /assume ram role/i }))

  expect(screen.getByLabelText(/role arn/i)).toBeInTheDocument()
  expect(screen.queryByLabelText(/ecs role name/i)).not.toBeInTheDocument()
})

it('does not hydrate an existing AssumeRole source credential into inputs', async () => {
  const user = userEvent.setup()
  const assumeModule: ConfigModule = {
    ...directModule,
    draft: {
      credential_mode: 'assume_role',
      assume_role: {
        source_access_key_id: { configured: true, display_hint: 'LTAI****WXYZ', updated_at: '2026-08-09T00:00:00Z' },
        source_access_key_secret: { configured: true, updated_at: '2026-08-09T00:00:00Z' },
        role_arn: 'acs:ram::1234567890123456:role/polardb-operator',
      },
    },
  }
  renderForm(assumeModule)

  expect(screen.getByText('LTAI****WXYZ')).toBeInTheDocument()
  expect(screen.queryByRole('textbox', { name: /source accesskey id/i })).not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: /replace source accesskey/i }))

  expect(screen.getByLabelText(/source accesskey id/i)).toHaveValue('')
  expect(screen.getByLabelText(/source accesskey secret/i)).toHaveValue('')
})

it('clears the previous credential by default when a credential mode changes', async () => {
  const user = userEvent.setup()
  const onSubmit = renderForm()

  await user.click(screen.getByRole('radio', { name: /ecs instance ram role/i }))
  await user.click(screen.getByRole('button', { name: 'Submit' }))

  expect(onSubmit).toHaveBeenCalledWith({
    credential_mode: 'ecs_ram_role',
    ecs_ram_role: { metadata_policy: 'v2_only' },
    transition: {
      previous_mode_action: 'clear',
      selected_mode_action: 'replace',
      reuse_direct_ak_as_assume_source: false,
      delete_retained_modes: [],
    },
  })
})

it('does not ask how to handle a current mode that has no stored credential', async () => {
  const user = userEvent.setup()
  const onSubmit = renderForm({
    ...directModule,
    revision: 0,
    workflow_state: 'NOT_CONFIGURED',
    draft: { credential_mode: 'direct_ak' },
  })

  await user.click(screen.getByRole('radio', { name: /ecs instance ram role/i }))

  expect(screen.queryByText(/current direct accesskey credential will stop/i)).not.toBeInTheDocument()
  expect(screen.queryByRole('radio', { name: /clear previous credential/i })).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Submit' }))
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
    credential_mode: 'ecs_ram_role',
    transition: expect.objectContaining({ previous_mode_action: 'clear' }),
  }))
})

it('identifies the current Direct credential before clearing or retaining it', async () => {
  const user = userEvent.setup()
  renderForm()

  await user.click(screen.getByRole('radio', { name: /ecs instance ram role/i }))

  const warning = transitionAlertFor(/current direct accesskey credential will stop/i)
  expect(within(warning).getByText('LTAI****ABCD')).toBeInTheDocument()
  expect(within(warning).getByRole('radio', { name: /clear previous/i })).toBeChecked()
})

it('identifies only the current AssumeRole source credential during a switch', async () => {
  const user = userEvent.setup()
  renderForm({
    ...directModule,
    draft: {
      credential_mode: 'assume_role',
      assume_role: {
        source_access_key_id: { configured: true, display_hint: 'LTAI****ROLE' },
        source_access_key_secret: 'source-secret-must-not-render',
        role_arn: 'acs:ram::1234567890123456:role/operator',
        external_id: 'external-id-must-not-render',
      },
    },
  })

  await user.click(screen.getByRole('radio', { name: /^direct accesskey uses/i }))

  const warning = transitionAlertFor(/current assume ram role credential will stop/i)
  expect(within(warning).getByText('LTAI****ROLE')).toBeInTheDocument()
  expect(within(warning).queryByText(/source-secret-must-not-render/)).not.toBeInTheDocument()
  expect(within(warning).queryByText(/external-id-must-not-render/)).not.toBeInTheDocument()
})

it.each([
  {
    block: { role_name: 'instance-role', metadata_policy: 'v2_only' },
    summary: /^instance-role$/i,
  },
  {
    block: { metadata_policy: 'v2_only' },
    summary: /ecs instance ram role discovered/i,
  },
])('identifies the current ECS role safely during a switch', async ({ block, summary }) => {
  const user = userEvent.setup()
  renderForm({
    ...directModule,
    draft: {
      credential_mode: 'ecs_ram_role',
      ecs_ram_role: block,
    },
  })

  await user.click(screen.getByRole('radio', { name: /^direct accesskey uses/i }))

  const warning = transitionAlertFor(/current ecs instance ram role credential will stop/i)
  expect(within(warning).getByText(summary)).toBeInTheDocument()
})

it('reuses an unchanged active credential without submitting its display markers', async () => {
  const user = userEvent.setup()
  const onSubmit = renderForm()

  await user.click(screen.getByRole('button', { name: 'Submit' }))

  expect(onSubmit).toHaveBeenCalledWith({
    credential_mode: 'direct_ak',
    transition: {
      previous_mode_action: 'clear',
      selected_mode_action: 'reuse_retained',
      reuse_direct_ak_as_assume_source: false,
      delete_retained_modes: [],
    },
  })
})

it('requires explicit confirmation before reusing a retained direct credential for AssumeRole', async () => {
  const user = userEvent.setup()
  const onSubmit = renderForm()

  await user.click(screen.getByRole('radio', { name: /assume ram role/i }))
  await user.click(screen.getByRole('radio', { name: /retain but disable/i }))
  await user.click(screen.getByRole('radio', { name: /use retained direct accesskey/i }))
  await user.click(screen.getByRole('checkbox', { name: /reuse the retained direct accesskey/i }))
  await user.type(screen.getByLabelText(/role arn/i), 'acs:ram::1234567890123456:role/polardb-operator')
  await user.click(screen.getByRole('button', { name: 'Submit' }))

  expect(onSubmit).toHaveBeenCalledWith({
    credential_mode: 'assume_role',
    assume_role: { role_arn: 'acs:ram::1234567890123456:role/polardb-operator' },
    transition: {
      previous_mode_action: 'retain',
      selected_mode_action: 'replace',
      reuse_direct_ak_as_assume_source: true,
      delete_retained_modes: [],
    },
  })
})

it('deletes a retained credential without changing the selected active mode', async () => {
  const user = userEvent.setup()
  const retainedModule: ConfigModule = {
    ...directModule,
    draft: {
      credential_mode: 'ecs_ram_role',
      ecs_ram_role: { metadata_policy: 'v2_only' },
      direct_ak: {
        access_key_id: { configured: true, display_hint: 'LTAI****ABCD', updated_at: '2026-08-09T00:00:00Z' },
        access_key_secret: { configured: true, updated_at: '2026-08-09T00:00:00Z' },
      },
    },
  }
  const onSubmit = renderForm(retainedModule)

  await user.click(screen.getByRole('button', { name: /delete retained direct accesskey credential/i }))
  await user.click(screen.getByRole('checkbox', { name: /confirm delete retained direct accesskey/i }))
  await user.click(screen.getByRole('button', { name: /confirm deletion/i }))

  expect(onSubmit).toHaveBeenCalledWith({
    credential_mode: 'ecs_ram_role',
    transition: {
      previous_mode_action: 'clear',
      selected_mode_action: 'reuse_retained',
      reuse_direct_ak_as_assume_source: false,
      delete_retained_modes: ['direct_ak'],
    },
  })
})

it('links to ECS role guidance and explains the ECS deployment requirement without protocol details', async () => {
  const user = userEvent.setup()
  renderForm()

  expect(screen.getByRole('link', { name: /accesskey documentation/i })).toHaveAttribute('target', '_blank')
  expect(screen.getByRole('link', { name: /assumerole documentation/i })).toHaveAttribute('target', '_blank')
  expect(screen.getByRole('link', { name: /ecs instance ram role documentation/i })).toHaveAttribute(
    'href',
    'https://www.alibabacloud.com/help/en/ecs/user-guide/attach-an-instance-ram-role-to-an-ecs-instance',
  )

  await user.click(screen.getByRole('radio', { name: /ecs instance ram role/i }))

  expect(screen.getByText('PAS must be deployed on an authorized ECS instance.')).toBeInTheDocument()
  expect(screen.queryByText(/IMDSv2/i)).not.toBeInTheDocument()
})

it('shows active ECS role discovery only when no explicit role name is persisted', () => {
  renderForm({
    ...directModule,
    draft: {
      credential_mode: 'ecs_ram_role',
      ecs_ram_role: { role_name: null, metadata_policy: 'v2_only' },
    },
  })

  expect(screen.getByText('ECS instance RAM role discovered')).toBeInTheDocument()

  cleanup()
  renderForm({
    ...directModule,
    draft: {
      credential_mode: 'ecs_ram_role',
      ecs_ram_role: { role_name: 'instance-role', metadata_policy: 'v2_only' },
    },
  })

  expect(screen.queryByText('ECS instance RAM role discovered')).not.toBeInTheDocument()
  expect(screen.getByLabelText(/ecs role name/i)).toHaveValue('instance-role')
})

it('submits a same-mode partial AssumeRole edit without replacing stored source secrets', async () => {
  const user = userEvent.setup()
  const onSubmit = vi.fn()
  const module: ConfigModule = {
    ...directModule,
    draft: {
      credential_mode: 'assume_role',
      assume_role: {
        source_access_key_id: { configured: true, display_hint: 'LTAI****WXYZ' },
        source_access_key_secret: { configured: true },
        role_arn: 'acs:ram::1234567890123456:role/old',
      },
    },
  }
  renderForm(module, onSubmit)

  await user.clear(screen.getByLabelText(/role arn/i))
  await user.type(screen.getByLabelText(/role arn/i), 'acs:ram::1234567890123456:role/new')
  await user.click(screen.getByRole('button', { name: 'Submit' }))

  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
    credential_mode: 'assume_role',
    assume_role: { role_arn: 'acs:ram::1234567890123456:role/new' },
    transition: expect.objectContaining({ selected_mode_action: 'replace' }),
  }))
})

it('requires an explicit choice before a retained target can be submitted', async () => {
  const user = userEvent.setup()
  const onSubmit = vi.fn()
  const module: ConfigModule = {
    ...directModule,
    draft: {
      credential_mode: 'ecs_ram_role',
      ecs_ram_role: { metadata_policy: 'v2_only' },
      direct_ak: {
        access_key_id: { configured: true, display_hint: 'LTAI****ABCD' },
        access_key_secret: { configured: true },
      },
    },
  }
  renderForm(module, onSubmit)

  await user.click(screen.getByRole('radio', { name: /^direct accesskey uses/i }))
  await user.click(screen.getByRole('button', { name: 'Submit' }))

  expect(onSubmit).not.toHaveBeenCalled()
  expect(screen.getByText(/choose whether/i)).toBeInTheDocument()
})

it('deletes a retained block against the current mode after an unrelated target selection', async () => {
  const user = userEvent.setup()
  const onSubmit = vi.fn()
  const module: ConfigModule = {
    ...directModule,
    draft: {
      credential_mode: 'ecs_ram_role',
      ecs_ram_role: { metadata_policy: 'v2_only' },
      direct_ak: { access_key_id: { configured: true, display_hint: 'LTAI****ABCD' }, access_key_secret: { configured: true } },
      assume_role: { source_access_key_id: { configured: true, display_hint: 'LTAI****WXYZ' }, source_access_key_secret: { configured: true }, role_arn: 'acs:ram::1234567890123456:role/test' },
    },
  }
  renderForm(module, onSubmit)

  await user.click(screen.getByRole('radio', { name: /^direct accesskey uses/i }))
  await user.click(screen.getByRole('button', { name: /delete retained assume ram role credential/i }))
  await user.click(screen.getByRole('checkbox', { name: /confirm delete retained assume ram role/i }))
  await user.click(screen.getByRole('button', { name: /confirm deletion/i }))

  expect(onSubmit).toHaveBeenCalledWith({
    credential_mode: 'ecs_ram_role',
    transition: {
      previous_mode_action: 'clear',
      selected_mode_action: 'reuse_retained',
      reuse_direct_ak_as_assume_source: false,
      delete_retained_modes: ['assume_role'],
    },
  })
})

it('submits an explicit null to clear an active ECS role name', async () => {
  const user = userEvent.setup()
  const onSubmit = vi.fn()
  renderForm({ ...directModule, draft: { credential_mode: 'ecs_ram_role', ecs_ram_role: { role_name: 'old-role', metadata_policy: 'v2_only' } } }, onSubmit)
  await user.clear(screen.getByLabelText(/ecs role name/i))
  await user.click(screen.getByRole('button', { name: 'Submit' }))
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ ecs_ram_role: { role_name: null, metadata_policy: 'v2_only' } }))
})

it('does not submit from an external button when disabled', async () => {
  const user = userEvent.setup()
  const onSubmit = vi.fn()
  render(<><AliyunAccessForm module={directModule} disabled onSubmit={onSubmit} formId="disabled-form" /><button type="submit" form="disabled-form">Submit</button></>)
  await user.click(screen.getByRole('button', { name: 'Submit' }))
  expect(onSubmit).not.toHaveBeenCalled()
})

it('resets dirty replacement state after moving away and back to a credential mode', async () => {
  const user = userEvent.setup()
  const onSubmit = renderForm()
  await user.click(screen.getByRole('button', { name: /replace accesskey/i }))
  await user.type(screen.getByLabelText(/accesskey id/i), 'replacement')
  await user.type(screen.getByLabelText(/accesskey secret/i), 'replacement-secret')
  await user.click(screen.getByRole('radio', { name: /assume ram role/i }))
  await user.click(screen.getByRole('radio', { name: /^direct accesskey uses/i }))
  await user.click(screen.getByRole('button', { name: 'Submit' }))
  expect(onSubmit).toHaveBeenLastCalledWith(expect.objectContaining({ transition: expect.objectContaining({ selected_mode_action: 'reuse_retained' }) }))
})

it('resets transient state when a newer module snapshot is rendered', async () => {
  const user = userEvent.setup()
  const onSubmit = vi.fn()
  const view = render(<AliyunAccessForm module={directModule} onSubmit={onSubmit} formId="snapshot-form" />)
  await user.click(screen.getByRole('button', { name: /replace accesskey/i }))
  await user.type(screen.getByLabelText(/accesskey id/i), 'replacement')
  view.rerender(<AliyunAccessForm module={{ ...directModule, revision: 3, draft: { ...directModule.draft, direct_ak: { access_key_id: { configured: true, display_hint: 'LTAI****NEXT' }, access_key_secret: { configured: true } } } }} onSubmit={onSubmit} formId="snapshot-form" />)
  expect(screen.getByText('LTAI****NEXT')).toBeInTheDocument()
  expect(screen.queryByLabelText(/accesskey id/i)).not.toBeInTheDocument()
})

it('uses locale-specific official documentation destinations', async () => {
  renderForm()
  expect(screen.getByRole('link', { name: /accesskey documentation/i })).toHaveAttribute('href', expect.stringContaining('/help/en/'))
  await i18n.changeLanguage('zh-CN')
  expect(screen.getByRole('link', { name: /accesskey 文档/i })).toHaveAttribute('href', expect.stringContaining('/zh/'))
  expect(screen.getByRole('radio', { name: /扮演 RAM 角色/ })).toBeInTheDocument()
  expect(screen.getByText(/临时身份凭证（STS Token）/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /ECS 实例 RAM 角色文档/i })).toHaveAttribute(
    'href',
    'https://help.aliyun.com/zh/ecs/user-guide/attach-an-instance-ram-role-to-an-ecs-instance',
  )
})

it('announces and focuses a missing direct-source reuse confirmation', async () => {
  const user = userEvent.setup()
  renderForm()
  await user.click(screen.getByRole('radio', { name: /assume ram role/i }))
  await user.click(screen.getByRole('radio', { name: /use retained direct accesskey/i }))
  await user.type(screen.getByLabelText(/role arn/i), 'acs:ram::1234567890123456:role/test')
  await user.click(screen.getByRole('button', { name: 'Submit' }))
  const alert = screen.getByText(/confirm that the direct accesskey/i).closest('[tabindex]')
  expect(alert).toHaveFocus()
  expect(alert?.querySelector('[role="alert"]')).toBeInTheDocument()
})

it('reports all primary payload-changing controls through onValuesChange', async () => {
  const user = userEvent.setup()
  const onValuesChange = vi.fn()
  render(<AliyunAccessForm module={directModule} onSubmit={vi.fn()} onValuesChange={onValuesChange} />)
  await user.click(screen.getByRole('radio', { name: /assume ram role/i }))
  await user.click(screen.getByRole('radio', { name: /retain but disable/i }))
  await user.click(screen.getByRole('radio', { name: /use retained direct accesskey/i }))
  await user.click(screen.getByRole('checkbox', { name: /reuse the retained direct accesskey/i }))
  expect(onValuesChange).toHaveBeenCalledTimes(4)
})

it('reports payload-changing field, replacement, and retained-delete controls through onValuesChange', async () => {
  const user = userEvent.setup()
  const onValuesChange = vi.fn()
  render(<AliyunAccessForm module={directModule} onSubmit={vi.fn()} onValuesChange={onValuesChange} />)

  await user.click(screen.getByRole('button', { name: /replace accesskey/i }))
  await user.type(screen.getByLabelText(/accesskey id/i), 'replacement')
  await user.type(screen.getByLabelText(/accesskey secret/i), 'replacement-secret')
  expect(onValuesChange).toHaveBeenCalled()
  expect(onValuesChange.mock.calls.length).toBeGreaterThan(2)
})

it('reports retained deletion selection and confirmation controls through onValuesChange', async () => {
  const user = userEvent.setup()
  const onValuesChange = vi.fn()
  render(<AliyunAccessForm module={{
    ...directModule,
    draft: {
      credential_mode: 'ecs_ram_role',
      ecs_ram_role: { metadata_policy: 'v2_only' },
      direct_ak: directModule.draft!.direct_ak,
    },
  }} onSubmit={vi.fn()} onValuesChange={onValuesChange} />)

  await user.click(screen.getByRole('button', { name: /delete retained direct accesskey credential/i }))
  await user.click(screen.getByRole('checkbox', { name: /confirm delete retained direct accesskey/i }))
  await user.click(screen.getByRole('button', { name: /cancel/i }))
  expect(onValuesChange).toHaveBeenCalledTimes(3)
})

it('reports every AssumeRole and ECS payload control through onValuesChange', async () => {
  const user = userEvent.setup()
  const onValuesChange = vi.fn()
  const expectChange = async (operation: () => Promise<void>) => {
    onValuesChange.mockClear()
    await operation()
    expect(onValuesChange).toHaveBeenCalled()
  }
  render(<AliyunAccessForm module={{
    ...directModule,
    draft: {
      credential_mode: 'assume_role',
      assume_role: {
        source_access_key_id: { configured: true, display_hint: 'LTAI****ROLE', updated_at: '2026-08-09T00:00:00Z' },
        source_access_key_secret: { configured: true },
        role_arn: 'acs:ram::1234567890123456:role/operator',
        external_id: { configured: true },
      },
    },
  }} onSubmit={vi.fn()} onValuesChange={onValuesChange} />)
  await expectChange(() => user.click(screen.getByRole('button', { name: /replace source accesskey/i })))
  await expectChange(() => user.type(screen.getByLabelText(/role arn/i), 'x'))
  await user.click(screen.getByText(/advanced assumerole settings/i))
  await expectChange(() => user.type(screen.getByLabelText(/role session name/i), 'session'))
  await expectChange(() => user.clear(screen.getByLabelText(/sts duration/i)))
  await expectChange(() => user.click(screen.getByRole('button', { name: /replace external id/i })))
  await expectChange(() => user.type(screen.getByLabelText(/external id/i), 'external'))

  cleanup()
  render(<AliyunAccessForm module={{
    ...directModule,
    draft: { credential_mode: 'ecs_ram_role', ecs_ram_role: { metadata_policy: 'v2_only' } },
  }} onSubmit={vi.fn()} onValuesChange={onValuesChange} />)
  await expectChange(() => user.type(screen.getByLabelText(/ecs role name/i), 'instance-role'))
})

it('reports both retained target actions through onValuesChange', async () => {
  const user = userEvent.setup()
  const onValuesChange = vi.fn()
  render(<AliyunAccessForm module={{
    ...directModule,
    draft: {
      credential_mode: 'ecs_ram_role',
      ecs_ram_role: { metadata_policy: 'v2_only' },
      direct_ak: directModule.draft!.direct_ak,
    },
  }} onSubmit={vi.fn()} onValuesChange={onValuesChange} />)
  await user.click(screen.getByRole('radio', { name: /^direct accesskey uses/i }))
  onValuesChange.mockClear()
  await user.click(screen.getByRole('radio', { name: /use retained credential/i }))
  expect(onValuesChange).toHaveBeenCalled()
  onValuesChange.mockClear()
  await user.click(screen.getByRole('radio', { name: /enter new credential/i }))
  expect(onValuesChange).toHaveBeenCalled()
})

it('keeps marker metadata in selected retained summaries for every credential mode', async () => {
  const user = userEvent.setup()
  const module: ConfigModule = {
    ...directModule,
    draft: {
      credential_mode: 'ecs_ram_role',
      ecs_ram_role: { role_name: 'instance-role', metadata_policy: 'v2_only' },
      direct_ak: directModule.draft!.direct_ak,
      assume_role: {
        source_access_key_id: { configured: true, display_hint: 'LTAI****ROLE', updated_at: '2026-08-09T00:00:00Z' },
        source_access_key_secret: { configured: true },
        role_arn: 'acs:ram::1234567890123456:role/operator',
      },
    },
  }
  renderForm(module)

  await user.click(screen.getByRole('radio', { name: /^direct accesskey uses/i }))
  expect(screen.getByText(/selected retained direct accesskey credential/i)).toBeInTheDocument()
  expect(screen.getByText('LTAI****ABCD')).toBeInTheDocument()
  expect(screen.getAllByText(/updated /i).length).toBeGreaterThan(0)
  await user.click(screen.getByRole('radio', { name: /^assume ram role pas/i }))
  expect(screen.getByText(/selected retained assume ram role credential/i)).toBeInTheDocument()
  expect(screen.getByText('LTAI****ROLE')).toBeInTheDocument()
  expect(screen.getAllByText(/updated /i).length).toBeGreaterThan(0)
  cleanup()
  render(<AliyunAccessForm module={{
    ...directModule,
    draft: {
      credential_mode: 'direct_ak',
      direct_ak: directModule.draft!.direct_ak,
      ecs_ram_role: { role_name: 'instance-role', metadata_policy: 'v2_only' },
    },
  }} onSubmit={vi.fn()} />)
  await user.click(screen.getByRole('radio', { name: /^ecs instance ram role pas must/i }))
  expect(screen.getByText(/selected retained ecs instance ram role credential/i)).toBeInTheDocument()
  expect(screen.getByText(/^instance-role$/i)).toBeInTheDocument()
})

it('blocks incomplete retained marker pairs while retaining their summary', async () => {
  const user = userEvent.setup()
  const module: ConfigModule = {
    ...directModule,
    draft: {
      credential_mode: 'ecs_ram_role',
      ecs_ram_role: { metadata_policy: 'v2_only' },
      direct_ak: { access_key_id: { configured: true, display_hint: 'LTAI****PARTIAL' } },
    },
  }
  renderForm(module)
  await user.click(screen.getByRole('radio', { name: /^direct accesskey uses/i }))

  expect(screen.getByText(/selected retained direct accesskey credential/i)).toBeInTheDocument()
  expect(screen.getByRole('radio', { name: /use retained credential/i })).toBeDisabled()
})

it('blocks Direct-to-AssumeRole source reuse when the active direct marker pair is incomplete', async () => {
  const user = userEvent.setup()
  renderForm({
    ...directModule,
    draft: {
      ...directModule.draft,
      direct_ak: { access_key_id: { configured: true, display_hint: 'LTAI****PARTIAL' } },
    },
  })
  await user.click(screen.getByRole('radio', { name: /^assume ram role pas/i }))
  expect(screen.getByRole('radio', { name: /use retained direct accesskey/i })).toBeDisabled()
})

it('omits a blank optional External ID for a new AssumeRole replacement', async () => {
  const user = userEvent.setup()
  const onSubmit = renderForm()
  await user.click(screen.getByRole('radio', { name: /^assume ram role pas/i }))
  await user.click(screen.getByRole('radio', { name: /use retained direct accesskey/i }))
  await user.click(screen.getByRole('checkbox', { name: /reuse the retained direct accesskey/i }))
  await user.type(screen.getByLabelText(/role arn/i), 'acs:ram::1234567890123456:role/operator')
  await user.click(screen.getByText(/advanced assumerole settings/i))
  await user.type(screen.getByLabelText(/external id/i), 'temporary')
  await user.clear(screen.getByLabelText(/external id/i))
  await user.click(screen.getByRole('button', { name: 'Submit' }))

  const mutation = onSubmit.mock.calls[0][0]
  expect(mutation.assume_role).not.toHaveProperty('external_id')
})

it('submits an explicit duration default and external-id clear action after advanced edits', async () => {
  const user = userEvent.setup()
  const onSubmit = vi.fn()
  const module: ConfigModule = {
    ...directModule,
    draft: {
      credential_mode: 'assume_role',
      assume_role: {
        source_access_key_id: { configured: true, display_hint: 'LTAI****ROLE' },
        source_access_key_secret: { configured: true },
        role_arn: 'acs:ram::1234567890123456:role/operator',
        duration_seconds: 1800,
        external_id: { configured: true },
      },
    },
  }
  renderForm(module, onSubmit)
  await user.click(screen.getByText(/advanced assumerole settings/i))
  const duration = screen.getByLabelText(/sts duration/i)
  await user.clear(duration)
  await user.click(screen.getByRole('button', { name: /replace external id/i }))
  await user.type(screen.getByLabelText(/external id/i), 'temporary')
  await user.clear(screen.getByLabelText(/external id/i))
  await user.click(screen.getByRole('button', { name: 'Submit' }))

  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
    assume_role: expect.objectContaining({
      duration_seconds: 3600,
      external_id: { $secret_action: 'clear' },
    }),
  }))
})

it('provides named advanced controls and a localized wrapped timestamp marker', async () => {
  const user = userEvent.setup()
  renderForm({
    ...directModule,
    draft: {
      credential_mode: 'assume_role',
      assume_role: {
        source_access_key_id: { configured: true, display_hint: 'LTAI****WXYZ', updated_at: '2026-08-09T00:00:00Z' },
        source_access_key_secret: { configured: true, updated_at: '2026-08-09T00:00:00Z' },
        role_arn: 'acs:ram::1234567890123456:role/operator',
      },
    },
  })
  expect(screen.getAllByText(/updated /i)[0].closest('.aliyun-access-form__marker')).toHaveClass('aliyun-access-form__marker')
  await user.click(screen.getByText(/advanced assumerole settings/i))
  expect(screen.getByLabelText(/role session name/i)).toHaveAttribute('id', 'aliyun-role-session-name')
  expect(screen.getByLabelText(/sts duration/i)).toHaveAttribute('id', 'aliyun-duration')
})
