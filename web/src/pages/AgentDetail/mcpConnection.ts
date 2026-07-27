export function buildMCPClientConfiguration(
  agentName: string,
  mcpUrl: string,
  token: string,
): string {
  return `${JSON.stringify(
    {
      mcpServers: {
        [agentName]: {
          url: mcpUrl,
          headers: {
            Authorization: `Bearer ${token}`,
          },
        },
      },
    },
    null,
    2,
  )}\n`
}
