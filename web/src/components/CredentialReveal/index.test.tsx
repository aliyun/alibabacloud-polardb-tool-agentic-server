import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import CredentialReveal from './index'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

describe('CredentialReveal', () => {
  it('clears a visible secret when the target changes', async () => {
    const user = userEvent.setup()
    const { rerender } = render(
      <CredentialReveal
        targetKey="credential-a"
        reveal={async () => ({
          username: 'target_a',
          password: 'target-a-secret',
          database_name: null,
        })}
      />,
    )

    await user.click(screen.getByRole('button', { name: /reveal credential/i }))
    await user.click(screen.getByRole('button', { name: /^confirm$/i }))
    expect(await screen.findByText('target-a-secret')).toBeInTheDocument()

    rerender(
      <CredentialReveal
        targetKey="credential-b"
        reveal={async () => ({
          username: 'target_b',
          password: 'target-b-secret',
          database_name: null,
        })}
      />,
    )

    await waitFor(() =>
      expect(screen.queryByText('target-a-secret')).not.toBeInTheDocument(),
    )
    expect(
      screen.queryByRole('dialog', { name: /revealed credential/i }),
    ).not.toBeInTheDocument()
  })

  it('never renders a pending response from a previous target', async () => {
    const user = userEvent.setup()
    const pendingA = deferred<{
      username: string
      password: string
      database_name: null
    }>()
    const revealA = vi.fn(() => pendingA.promise)
    const revealB = vi.fn().mockResolvedValue({
      username: 'target_b',
      password: 'target-b-secret',
      database_name: null,
    })
    const { rerender } = render(
      <CredentialReveal targetKey="credential-a" reveal={revealA} />,
    )

    await user.click(screen.getByRole('button', { name: /reveal credential/i }))
    await user.click(screen.getByRole('button', { name: /^confirm$/i }))
    expect(revealA).toHaveBeenCalledTimes(1)

    rerender(
      <CredentialReveal targetKey="credential-b" reveal={revealB} />,
    )
    await act(async () => {
      pendingA.resolve({
        username: 'target_a',
        password: 'late-target-a-secret',
        database_name: null,
      })
      await pendingA.promise
    })

    expect(screen.queryByText('late-target-a-secret')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /reveal credential/i })).toBeEnabled()
  })

  it('coalesces repeated confirmation clicks into one request', async () => {
    const user = userEvent.setup()
    const pending = deferred<{
      username: string
      password: string
      database_name: null
    }>()
    const reveal = vi.fn(() => pending.promise)
    render(<CredentialReveal targetKey="credential-a" reveal={reveal} />)

    await user.click(screen.getByRole('button', { name: /reveal credential/i }))
    const confirm = screen.getByRole('button', { name: /^confirm$/i })
    await act(async () => {
      confirm.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      confirm.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })

    expect(reveal).toHaveBeenCalledTimes(1)
    pending.resolve({
      username: 'target_a',
      password: 'one-request-secret',
      database_name: null,
    })
    expect(await screen.findByText('one-request-secret')).toBeInTheDocument()
  })

  it('reveals when mounted under React StrictMode', async () => {
    const user = userEvent.setup()
    render(
      <StrictMode>
        <CredentialReveal
          targetKey="strict-credential"
          reveal={async () => ({
            username: 'strict_user',
            password: 'strict-secret',
            database_name: null,
          })}
        />
      </StrictMode>,
    )

    await user.click(screen.getByRole('button', { name: /reveal credential/i }))
    await user.click(screen.getByRole('button', { name: /^confirm$/i }))
    expect(await screen.findByText('strict-secret')).toBeInTheDocument()
  })

  it('reveals only after explicit confirmation and never persists the secret', async () => {
    const user = userEvent.setup()
    const reveal = vi.fn().mockResolvedValue({
      username: 'readonly_user',
      password: 'test-secret',
      database_name: 'orders',
    })

    render(<CredentialReveal targetKey="credential-a" reveal={reveal} />)

    await user.click(screen.getByRole('button', { name: /reveal credential/i }))
    expect(reveal).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: /^confirm$/i }))

    expect(reveal).toHaveBeenCalledTimes(1)
    expect(reveal).toHaveBeenCalledWith({ confirmed: true })
    expect(await screen.findByText('test-secret')).toBeInTheDocument()
    expect(localStorage).toHaveLength(0)
    expect(sessionStorage).toHaveLength(0)
  })

  it('clears plaintext when the reveal panel closes and does not refetch on reopen', async () => {
    const user = userEvent.setup()
    const reveal = vi.fn().mockResolvedValue({
      username: 'readonly_user',
      password: 'temporary-secret',
      database_name: null,
    })

    render(<CredentialReveal targetKey="credential-a" reveal={reveal} />)
    await user.click(screen.getByRole('button', { name: /reveal credential/i }))
    await user.click(screen.getByRole('button', { name: /^confirm$/i }))
    expect(await screen.findByText('temporary-secret')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /close revealed credential/i }))
    expect(screen.queryByText('temporary-secret')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /reveal credential/i }))
    expect(reveal).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('temporary-secret')).not.toBeInTheDocument()
  })

  it('removes plaintext on unmount', async () => {
    const user = userEvent.setup()
    const reveal = vi.fn().mockResolvedValue({
      username: 'readonly_user',
      password: 'unmount-secret',
      database_name: null,
    })

    const view = render(
      <CredentialReveal targetKey="credential-a" reveal={reveal} />,
    )
    await user.click(screen.getByRole('button', { name: /reveal credential/i }))
    await user.click(screen.getByRole('button', { name: /^confirm$/i }))
    expect(await screen.findByText('unmount-secret')).toBeInTheDocument()

    view.unmount()
    expect(screen.queryByText('unmount-secret')).not.toBeInTheDocument()
  })

  it('provides a labelled copy action without browser persistence', async () => {
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    render(
      <CredentialReveal
        targetKey="credential-a"
        reveal={async () => ({
          username: 'copy_user',
          password: 'copy-secret',
          database_name: null,
        })}
      />,
    )

    await user.click(screen.getByRole('button', { name: /reveal credential/i }))
    await user.click(screen.getByRole('button', { name: /^confirm$/i }))
    await user.click(
      await screen.findByRole('button', { name: /copy password/i }),
    )

    expect(writeText).toHaveBeenCalledWith('copy-secret')
    expect(await screen.findByRole('status')).toHaveTextContent(/password copied/i)
    expect(localStorage).toHaveLength(0)
    expect(sessionStorage).toHaveLength(0)
  })

  it('clears a prior plaintext value when a later reveal fails', async () => {
    const user = userEvent.setup()
    const reveal = vi
      .fn()
      .mockResolvedValueOnce({
        username: 'readonly_user',
        password: 'stale-secret',
        database_name: null,
      })
      .mockRejectedValueOnce(new Error('request failed'))

    render(<CredentialReveal targetKey="credential-a" reveal={reveal} />)
    await user.click(screen.getByRole('button', { name: /reveal credential/i }))
    await user.click(screen.getByRole('button', { name: /^confirm$/i }))
    expect(await screen.findByText('stale-secret')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /close revealed credential/i }))
    await user.click(screen.getByRole('button', { name: /reveal credential/i }))
    await user.click(screen.getByRole('button', { name: /^confirm$/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/could not reveal credential/i)
    expect(screen.queryByText('stale-secret')).not.toBeInTheDocument()
  })
})
