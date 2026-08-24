import { useCallback } from 'react'

import type { ConfigModule, ConfigResponse } from '../../api/configuration'
import {
  createIdempotencyKey,
  getResponseRevision,
  parseActivationProof,
} from './workflowSafety'

export interface ActivationCandidate {
  moduleName: string
  revision: number
  config: Record<string, unknown>
  activationConfig?: Record<string, unknown>
  confirmExternalFailure: boolean
}

interface UseActivationWorkflowOptions {
  candidate?: ActivationCandidate
  selected?: ConfigModule
  bootstrapToken?: string
  execute: (
    command: {
      action: string
      module?: string
      expected_revision?: number
      validation_id?: string
      idempotency_key?: string
      confirm_impact?: boolean
      config?: Record<string, unknown>
    },
    bootstrapToken?: string,
  ) => Promise<ConfigResponse>
  loadModules: (bootstrapToken?: string) => Promise<ConfigResponse>
  loginCoreAdmin: (candidate: ActivationCandidate) => Promise<void>
  invalidate: () => void
  setBusy: (busy: boolean) => void
  onFailureAfterRefresh: () => void
  onSuccessAfterRefresh: () => void
  setCompleted: (completed: boolean) => void
  showError: (error: unknown, fallback: string) => void
  showProofError: () => void
  showSuccess: (module: string) => void
}

export function useActivationWorkflow({
  candidate,
  selected,
  bootstrapToken,
  execute,
  loadModules,
  loginCoreAdmin,
  invalidate,
  setBusy,
  onFailureAfterRefresh,
  onSuccessAfterRefresh,
  setCompleted,
  showError,
  showProofError,
  showSuccess,
}: UseActivationWorkflowOptions) {
  return useCallback(async (confirmExternalFailure = false) => {
    if (
      !candidate
      || !selected
      || candidate.moduleName !== selected.name
      || candidate.revision !== selected.revision
      || (candidate.confirmExternalFailure && !confirmExternalFailure)
    ) {
      invalidate()
      return
    }

    setBusy(true)
    let mutationStarted = false
    let proofFailure = false
    try {
      mutationStarted = true
      const saved = await execute({
        action: 'save_draft',
        module: candidate.moduleName,
        expected_revision: candidate.revision,
        config: candidate.config,
      }, bootstrapToken)
      const savedRevision = getResponseRevision(saved)
      if (savedRevision === undefined) {
        proofFailure = true
        throw new Error('invalid activation proof')
      }

      const validated = await execute({
        action: 'validate',
        module: candidate.moduleName,
        expected_revision: savedRevision,
        confirm_impact: candidate.confirmExternalFailure,
      }, bootstrapToken)
      const proof = parseActivationProof(saved, validated)
      if (!proof) {
        proofFailure = true
        throw new Error('invalid activation proof')
      }

      const activated = await execute({
        action: 'activate',
        module: candidate.moduleName,
        expected_revision: proof.validatedRevision,
        validation_id: proof.validationId,
        idempotency_key: createIdempotencyKey(),
        config: candidate.activationConfig,
      }, bootstrapToken)
      if (candidate.moduleName === 'core_admin') {
        await loginCoreAdmin(candidate)
      }
      showSuccess(candidate.moduleName)
      invalidate()
      await loadModules(candidate.moduleName === 'core_admin' ? undefined : bootstrapToken)
      if (activated.system_state === 'READY' && candidate.moduleName === 'core_admin') {
        setCompleted(true)
      }
      onSuccessAfterRefresh()
    } catch (error) {
      invalidate()
      if (proofFailure) {
        showProofError()
      } else {
        showError(error, 'activate')
      }
      if (mutationStarted) {
        try {
          await loadModules(bootstrapToken)
        } catch (refreshError) {
          showError(refreshError, 'refresh')
        }
      }
      onFailureAfterRefresh()
    } finally {
      setBusy(false)
    }
  }, [
    bootstrapToken,
    candidate,
    execute,
    invalidate,
    loadModules,
    loginCoreAdmin,
    onFailureAfterRefresh,
    onSuccessAfterRefresh,
    selected,
    setBusy,
    setCompleted,
    showError,
    showProofError,
    showSuccess,
  ])
}
