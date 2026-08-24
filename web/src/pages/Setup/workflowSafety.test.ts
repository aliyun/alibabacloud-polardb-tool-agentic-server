import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  createIdempotencyKey,
  getResponseRevision,
  normalizeDryRunDetails,
  parseActivationProof,
} from './workflowSafety'

afterEach(() => {
  vi.unstubAllGlobals()
})

it('creates an idempotency key when randomUUID is unavailable over HTTP', () => {
  vi.stubGlobal('crypto', undefined)

  expect(createIdempotencyKey()).toMatch(/^pas-/)
})

describe('normalizeDryRunDetails', () => {
  it('keeps only reviewed Aliyun fields and bounded values', () => {
    const details = normalizeDryRunDetails({
      valid: false,
      error_code: 'OPENAPI_CONNECT_FAILURE',
      request_id: 'request-123',
      external_validation: {
        checks: [
          {
            service: 'sts',
            endpoint: 'sts.cn-hangzhou.aliyuncs.com',
            status: 'REACHABLE',
            identity_hint: 'ro***',
            expires_at: 1_800_000_000,
            request_id: 'sts-request-1',
          },
          {
            service: 'ecs_metadata',
            endpoint: 'http://100.100.100.200',
            status: 'REACHABLE',
            identity_hint: '**',
          },
        ],
      },
    }, 'assume_role')

    expect(details).toEqual({
      valid: false,
      credentialMode: 'assume_role',
      errorCode: 'OPENAPI_CONNECT_FAILURE',
      requestId: 'request-123',
      checks: [
        {
          service: 'sts',
          endpoint: 'sts.cn-hangzhou.aliyuncs.com',
          status: 'REACHABLE',
          identityHint: 'ro***',
          expiresAt: 1_800_000_000,
          requestId: 'sts-request-1',
        },
        {
          service: 'ecs_metadata',
          endpoint: 'http://100.100.100.200',
          status: 'REACHABLE',
          identityHint: '**',
        },
      ],
      guidance: 'external',
    })
  })

  it('accepts only exact server-generated AccessKey identity masks', () => {
    const details = normalizeDryRunDetails({
      valid: true,
      external_validation: {
        checks: [
          {
            service: 'polardb',
            endpoint: 'polardb.aliyuncs.com',
            status: 'REACHABLE',
            identity_hint: 'LTAI****ABCD',
          },
          {
            service: 'polardb',
            endpoint: 'polardb.cn-hangzhou.aliyuncs.com',
            status: 'REACHABLE',
            identity_hint: '****',
          },
          {
            service: 'polardb',
            endpoint: 'polardb-vpc.cn-hangzhou.aliyuncs.com',
            status: 'REACHABLE',
            identity_hint: 'TEST1234567890ABCD',
          },
          {
            service: 'polardb',
            endpoint: 'polardb.cn-beijing.aliyuncs.com',
            status: 'REACHABLE',
            identity_hint: 'LTAI****<script>',
          },
          {
            service: 'polardb',
            endpoint: 'polardb.cn-shanghai.aliyuncs.com',
            status: 'REACHABLE',
            identity_hint: 'LT.A****ABCD',
          },
        ],
      },
    }, 'direct_ak')

    expect(details.checks.map((check) => check.identityHint)).toEqual([
      'LTAI****ABCD',
      '****',
      undefined,
      undefined,
      undefined,
    ])
  })

  it('drops malformed, credential-shaped, and unreviewed nested response data', () => {
    const details = normalizeDryRunDetails({
      valid: false,
      error_code: 'OPENAPI_ENDPOINT_UNSUPPORTED',
      message: 'access_key_secret=must-not-render',
      request_id: 'secret=must-not-render',
      external_validation: {
        checks: [
          null,
          {
            service: 'polardb',
            endpoint: 'access_key_secret=must-not-render',
            status: 'REACHABLE',
            identity_hint: 'access_key_secret=must-not-render',
            expires_at: 9_999_999_999_999,
            request_id: 'secret=must-not-render',
          },
          { service: 'sts', endpoint: 'sts.cn-hangzhou.aliyuncs.com', status: 'PASSED' },
        ],
      },
    }, 'direct_ak')

    expect(details).toEqual({
      valid: false,
      credentialMode: 'direct_ak',
      errorCode: 'OPENAPI_ENDPOINT_UNSUPPORTED',
      checks: [
        { service: 'sts', endpoint: 'sts.cn-hangzhou.aliyuncs.com', status: 'PASSED' },
      ],
      guidance: 'endpoint',
    })
  })

  it('rejects regionless, oversized, and malformed official-looking OpenAPI hosts', () => {
    const oversizedRegion = `cn-${'a'.repeat(62)}`
    const details = normalizeDryRunDetails({
      valid: true,
      external_validation: {
        checks: [
          { service: 'polardb', endpoint: 'polardb-vpc.aliyuncs.com', status: 'REACHABLE' },
          { service: 'polardb', endpoint: `polardb.${oversizedRegion}.aliyuncs.com`, status: 'REACHABLE' },
          { service: 'sts', endpoint: `sts.${oversizedRegion}.aliyuncs.com`, status: 'REACHABLE' },
          { service: 'polardb', endpoint: 'polardb.cn--hangzhou.aliyuncs.com', status: 'REACHABLE' },
          { service: 'sts', endpoint: 'sts.cn-hangzhou.aliyuncs.com.evil.example', status: 'REACHABLE' },
        ],
      },
    }, undefined)

    expect(details.checks).toEqual([])
  })

  it('accepts only the documented global and regional Aliyun hostname shapes', () => {
    const details = normalizeDryRunDetails({
      valid: true,
      external_validation: {
        checks: [
          { service: 'polardb', endpoint: 'polardb.aliyuncs.com', status: 'REACHABLE' },
          { service: 'polardb', endpoint: 'polardb.cn-hangzhou.aliyuncs.com', status: 'REACHABLE' },
          { service: 'polardb', endpoint: 'polardb-vpc.cn-hangzhou.aliyuncs.com', status: 'REACHABLE' },
          { service: 'sts', endpoint: 'sts.cn-hangzhou.aliyuncs.com', status: 'REACHABLE' },
          { service: 'sts', endpoint: 'sts-vpc.cn-hangzhou.aliyuncs.com', status: 'REACHABLE' },
          { service: 'ecs_metadata', endpoint: 'http://100.100.100.200', status: 'REACHABLE' },
        ],
      },
    }, undefined)

    expect(details.checks).toHaveLength(6)
  })

  it.each([
    ['INVALID_ADMIN_PASSWORD', 'adminPassword'],
    ['INVALID_MODULE_CONFIG', 'moduleConfig'],
    ['EXTERNAL_BASE_URL_REQUIRED', 'externalBaseUrl'],
    ['DEPENDENCY_NOT_ACTIVE', 'dependency'],
  ])('maps stable %s errors to actionable guidance', (errorCode, guidance) => {
    expect(normalizeDryRunDetails({ valid: false, error_code: errorCode }, undefined))
      .toMatchObject({ errorCode, guidance })
  })
})

describe('parseActivationProof', () => {
  it('accepts only an integer save revision', () => {
    expect(getResponseRevision({ module: { revision: 11 } })).toBe(11)
    expect(getResponseRevision({ module: { revision: '11' } })).toBeUndefined()
    expect(getResponseRevision({ module: null })).toBeUndefined()
  })

  it('returns the only activation proof shape accepted by the UI', () => {
    expect(parseActivationProof(
      { module: { revision: 11 } },
      {
        module: { revision: 13 },
        validation: { status: 'PASSED', validation_id: 'returned-proof' },
      },
    )).toEqual({ savedRevision: 11, validatedRevision: 13, validationId: 'returned-proof' })
  })

  it.each([
    [{ module: { revision: '11' } }, { module: { revision: 13 }, validation: { status: 'PASSED', validation_id: 'proof' } }],
    [{ module: { revision: 11 } }, { module: { revision: '13' }, validation: { status: 'PASSED', validation_id: 'proof' } }],
    [{ module: { revision: 11 } }, { module: { revision: 13 }, validation: { status: 'FAILED', validation_id: 'proof' } }],
    [{ module: { revision: 11 } }, { module: { revision: 13 }, validation: { status: 'PASSED', validation_id: '' } }],
    [{ module: { revision: 11 } }, { module: { revision: 13 }, validation: { status: 'PASSED' } }],
  ])('rejects malformed or failed proof responses', (saved, validated) => {
    expect(parseActivationProof(saved, validated)).toBeUndefined()
  })
})
