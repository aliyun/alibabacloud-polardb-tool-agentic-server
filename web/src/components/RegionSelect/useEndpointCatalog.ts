import { useEffect, useState } from 'react'
import { getEndpointCatalog, type EndpointCatalog, type OpenAPINetwork } from '../../api/aliyunEndpoints'

export function useEndpointCatalog(supplied?: EndpointCatalog, enabled = true) {
  const [catalog, setCatalog] = useState<EndpointCatalog | undefined>(supplied)
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    if (supplied || !enabled) {
      setCatalog(supplied)
      return
    }
    let active = true
    setFailed(false)
    setCatalog(undefined)
    void getEndpointCatalog().then(
      (value) => { if (active) setCatalog(value) },
      () => { if (active) setFailed(true) },
    )
    return () => { active = false }
  }, [supplied, enabled, attempt])
  return { catalog: supplied ?? catalog, failed, retry: () => setAttempt((value) => value + 1) }
}

export function regionAvailable(
  catalog: EndpointCatalog | undefined, region: string | undefined,
  network: OpenAPINetwork | undefined, requireSTS = false, accessRegion?: string,
) {
  const entry = catalog?.regions.find((item) => item.region_id === region)
  return Boolean(entry && network && entry.polardb[network]
    && (!requireSTS || entry.sts[network])
    && (!accessRegion || network !== 'vpc' || region === accessRegion))
}
