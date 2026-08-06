import { Checkbox, Flex, Typography } from 'antd'
import { useTranslation } from 'react-i18next'

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
}> = [
  {
    value: 'db_instance:list',
  },
  {
    value: 'db_instance:describe',
  },
  {
    value: 'db_instance:credentials:read',
  },
]

const CAPABILITY_KEYS = {
  'db_instance:list': ['components.capabilities.list', 'components.capabilities.listDescription'],
  'db_instance:describe': ['components.capabilities.metadata', 'components.capabilities.metadataDescription'],
  'db_instance:credentials:read': ['components.capabilities.credentials', 'components.capabilities.credentialsDescription'],
} as const

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
  const { t } = useTranslation()
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
    <Flex vertical gap={12} role="group" aria-label={t('components.capabilities.groupLabel')}>
      {CAPABILITIES.map((capability) => {
        const [labelKey, descriptionKey] = CAPABILITY_KEYS[capability.value]
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
              {t(labelKey)}
            </Checkbox>
            <Text
              id={descriptionId}
              type="secondary"
              style={{ paddingInlineStart: 24 }}
            >
              {t(descriptionKey)}
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
            {t('components.capabilities.sqlProxy')}
          </Checkbox>
          <Text
            id="capability-sql-proxy"
            type="secondary"
            style={{ paddingInlineStart: 24 }}
          >
            {t('components.capabilities.sqlProxyDescription')}
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
            {t('components.capabilities.create')}
          </Checkbox>
          <Text
            id="capability-managed-database-create"
            type="secondary"
            style={{ paddingInlineStart: 24 }}
          >
            {createAvailable ? (
              <>
                {t('components.capabilities.createDescription')}
              </>
            ) : (
              <>
                {t('components.capabilities.configureFirst')}{' '}
                <a href={`/instances/${instance.id}`}>{t('components.capabilities.configureBackend')}</a>
              </>
            )}
          </Text>
        </Flex>
      )}
    </Flex>
  )
}
