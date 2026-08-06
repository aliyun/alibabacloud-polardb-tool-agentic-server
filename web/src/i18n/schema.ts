import type { TFunction } from 'i18next'

export function schemaFieldLabel(
  t: TFunction,
  moduleName: string,
  fieldName: string,
  fallback: string,
): string {
  return t(
    `components.configuration.fields.${moduleName}.${fieldName}`,
    { defaultValue: fallback },
  )
}
