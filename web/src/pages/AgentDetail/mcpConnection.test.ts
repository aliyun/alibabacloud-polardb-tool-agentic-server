import { describe, expect, it } from 'vitest'

import { buildMCPClientConfiguration } from './mcpConnection'

describe('Agent MCP client configuration', () => {
  it('serializes the Agent name, endpoint, and Token as portable JSON', () => {
    expect(
      buildMCPClientConfiguration(
        'reader "production"',
        'https://console.example.com/mcp',
        'pas_agent_secret',
      ),
    ).toBe(`{
  "mcpServers": {
    "reader \\"production\\"": {
      "url": "https://console.example.com/mcp",
      "headers": {
        "Authorization": "Bearer pas_agent_secret"
      }
    }
  }
}
`)
  })
})
