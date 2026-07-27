import { Checkbox, Flex, Typography } from 'antd'

import type {
  AgentInstanceAccessCapability,
  DirectBindingCapability,
  Permission,
  SqlCapability,
} from '../../api/instanceAccess'
import type { InstanceSummary } from '../../api/instances'
import type { ProvisioningBackend } from '../../api/provisioningBackends'

const { Text } = Typography

const CAPABILITIES: Array<{
  value: DirectBindingCapability
  label: string
  description: string
}> = [
  {
    value: 'db_instance:list',
    label: 'List bound instances',
    description: 'See this instance in the Agent database inventory.',
  },
  {
    value: 'db_instance:describe',
    label: 'View instance metadata',
    description: 'Inspect connection metadata. Requires list access.',
  },
  {
    value: 'db_instance:credentials:read',
    label: 'Reveal credentials',
    description: 'Retrieve connection credentials. Requires metadata access.',
  },
]

const DEPENDENCIES: Record<
  DirectBindingCapability,
  DirectBindingCapability[]
> = {
  'db_instance:list': [],
  'db_instance:describe': ['db_instance:list'],
  'db_instance:credentials:read': [
    'db_instance:list',
    'db_instance:describe',
  ],
}

const SQL_CAPABILITIES: SqlCapability[] = ['sql:read', 'sql:write']

function sqlCapabilities(permission: Permission): SqlCapability[] {
  return permission === 'readwrite'
    ? SQL_CAPABILITIES
    : ['sql:read']
}

function ordered(
  values: Set<AgentInstanceAccessCapability>,
): AgentInstanceAccessCapability[] {
  return [
    ...CAPABILITIES.map(({ value }) => value),
    ...SQL_CAPABILITIES,
    'db_instance:create' as const,
  ].filter((value) => values.has(value))
}

function dependsOn(
  capability: DirectBindingCapability,
  dependency: DirectBindingCapability,
) {
  return DEPENDENCIES[capability].includes(dependency)
}

export interface CapabilityEditorProps<
  T extends AgentInstanceAccessCapability = DirectBindingCapability,
> {
  permission?: Permission
  value: T[]
  onChange: (capabilities: T[]) => void
  disabled?: boolean
  instance?: InstanceSummary
  provisioningBackend?: ProvisioningBackend
}

export default function CapabilityEditor<
  T extends AgentInstanceAccessCapability = DirectBindingCapability,
>({
  permission,
  value,
  onChange,
  disabled = false,
  instance,
  provisioningBackend,
}: CapabilityEditorProps<T>) {
  const selected = new Set<AgentInstanceAccessCapability>(value)
  const sqlProxyEnabled = SQL_CAPABILITIES.some((capability) =>
    selected.has(capability),
  )
  const canOfferCreate =
    instance?.engine === 'polardb_mysql' &&
    instance.topology === 'multitenant' &&
    instance.allocation_mode === 'registered' &&
    instance.status === 'active'
  const createAvailable =
    provisioningBackend?.available_for_create === true

  const handleToggle = (
    capability: DirectBindingCapability,
    checked: boolean,
  ) => {
    const next = new Set(selected)
    if (checked) {
      next.add(capability)
      DEPENDENCIES[capability].forEach((dependency) => next.add(dependency))
    } else {
      next.delete(capability)
      CAPABILITIES.forEach(({ value: candidate }) => {
        if (dependsOn(candidate, capability)) {
          next.delete(candidate)
        }
      })
    }
    onChange(ordered(next) as T[])
  }

  const handleCreateToggle = (checked: boolean) => {
    const next = new Set(selected)
    if (checked) {
      next.add('db_instance:create')
    } else {
      next.delete('db_instance:create')
    }
    onChange(ordered(next) as T[])
  }

  const handleSqlProxyToggle = (checked: boolean) => {
    const next = new Set(selected)
    SQL_CAPABILITIES.forEach((capability) => next.delete(capability))
    if (checked && permission) {
      sqlCapabilities(permission).forEach((capability) =>
        next.add(capability),
      )
    }
    onChange(ordered(next) as T[])
  }

  return (
    <Flex vertical gap={12} role="group" aria-label="Instance capabilities">
      {CAPABILITIES.map((capability) => {
        const descriptionId = `capability-${capability.value.replace(/:/g, '-')}`
        return (
          <Flex vertical gap={2} key={capability.value}>
            <Checkbox
              checked={selected.has(capability.value)}
              disabled={disabled}
              aria-describedby={descriptionId}
              onChange={(event) =>
                handleToggle(capability.value, event.target.checked)
              }
            >
              {capability.label}
            </Checkbox>
            <Text
              id={descriptionId}
              type="secondary"
              style={{ paddingInlineStart: 24 }}
            >
              {capability.description}
            </Text>
          </Flex>
        )
      })}
      {permission && (
        <Flex vertical gap={2}>
          <Checkbox
            checked={sqlProxyEnabled}
            disabled={disabled}
            aria-describedby="capability-sql-proxy"
            onChange={(event) =>
              handleSqlProxyToggle(event.target.checked)
            }
          >
            Enable SQL over HTTP proxy
          </Checkbox>
          <Text
            id="capability-sql-proxy"
            type="secondary"
            style={{ paddingInlineStart: 24 }}
          >
            Enables run_sql, run_sql_transaction, and describe_schema
            through the SQL over HTTP proxy. Accessible databases and
            operations remain limited by the selected MySQL account.
          </Text>
        </Flex>
      )}
      {canOfferCreate && (
        <Flex vertical gap={2}>
          <Checkbox
            checked={selected.has('db_instance:create')}
            disabled={disabled || !createAvailable}
            aria-describedby="capability-managed-database-create"
            onChange={(event) =>
              handleCreateToggle(event.target.checked)
            }
          >
            Create managed databases
          </Checkbox>
          <Text
            id="capability-managed-database-create"
            type="secondary"
            style={{ paddingInlineStart: 24 }}
          >
            {createAvailable ? (
              <>
                Allows create_db_instance to provision an isolated database
                and account on this multitenant instance.
              </>
            ) : (
              <>
                Configure a provisioning backend for this instance first.{' '}
                <a href={`/instances/${instance.id}`}>Configure backend</a>
              </>
            )}
          </Text>
        </Flex>
      )}
    </Flex>
  )
}
