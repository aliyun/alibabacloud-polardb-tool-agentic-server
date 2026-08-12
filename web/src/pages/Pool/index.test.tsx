import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import Pool from './index'

vi.mock('./DedicatedPoolPanel', () => ({
  default: () => <div>Dedicated pools</div>,
}))

describe('Pool', () => {
  it('renders only the Dedicated pool surface', () => {
    render(<Pool />)

    expect(screen.getByText('Dedicated pools')).toBeInTheDocument()
    expect(screen.queryByRole('tab')).not.toBeInTheDocument()
    expect(screen.queryByText(/user instance hot pool/i)).not.toBeInTheDocument()
  })
})
