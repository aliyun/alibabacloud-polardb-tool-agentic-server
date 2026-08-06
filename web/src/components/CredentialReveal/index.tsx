import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Descriptions,
  Modal,
  Space,
  Typography,
} from 'antd'
import { useTranslation } from 'react-i18next'

import type {
  CredentialRevealRequest,
  RevealedCredential,
} from '../../api/credentials'

const { Text } = Typography

export interface CredentialRevealProps {
  targetKey: string
  reveal: (
    request: CredentialRevealRequest,
  ) => Promise<RevealedCredential>
  disabled?: boolean
}

export default function CredentialReveal({
  targetKey,
  reveal,
  disabled = false,
}: CredentialRevealProps) {
  const { t } = useTranslation()
  const mountedRef = useRef(true)
  const generationRef = useRef(0)
  const inFlightGenerationRef = useRef<number | null>(null)
  const [confirmationOpen, setConfirmationOpen] = useState(false)
  const [revealedOpen, setRevealedOpen] = useState(false)
  const [revealed, setRevealed] = useState<RevealedCredential | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copyStatus, setCopyStatus] = useState<string | null>(null)

  const clearPlaintext = () => {
    setRevealed(null)
    setCopyStatus(null)
  }

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      inFlightGenerationRef.current = null
    }
  }, [])

  useLayoutEffect(() => {
    generationRef.current += 1
    inFlightGenerationRef.current = null
    setConfirmationOpen(false)
    setRevealedOpen(false)
    setRevealed(null)
    setLoading(false)
    setError(null)
    setCopyStatus(null)
  }, [targetKey])

  const beginReveal = () => {
    clearPlaintext()
    setError(null)
    setConfirmationOpen(true)
  }

  const confirmReveal = async () => {
    const operationGeneration = generationRef.current
    if (inFlightGenerationRef.current === operationGeneration) return
    inFlightGenerationRef.current = operationGeneration
    clearPlaintext()
    setError(null)
    setLoading(true)
    try {
      const plaintext = await reveal({ confirmed: true })
      if (
        !mountedRef.current ||
        generationRef.current !== operationGeneration ||
        inFlightGenerationRef.current !== operationGeneration
      ) {
        return
      }
      setRevealed(plaintext)
      setConfirmationOpen(false)
      setRevealedOpen(true)
    } catch {
      if (
        !mountedRef.current ||
        generationRef.current !== operationGeneration ||
        inFlightGenerationRef.current !== operationGeneration
      ) {
        return
      }
      clearPlaintext()
      setConfirmationOpen(false)
      setError(t('components.credentialReveal.loadFailed'))
    } finally {
      if (
        mountedRef.current &&
        generationRef.current === operationGeneration &&
        inFlightGenerationRef.current === operationGeneration
      ) {
        inFlightGenerationRef.current = null
        setLoading(false)
      }
    }
  }

  const closeRevealed = () => {
    setRevealedOpen(false)
    clearPlaintext()
  }

  const copySecret = async (label: string, value: string) => {
    try {
      if (!navigator.clipboard) {
        throw new Error('Clipboard unavailable')
      }
      await navigator.clipboard.writeText(value)
      setCopyStatus(t('components.credentialReveal.copied', { label }))
    } catch {
      setCopyStatus(t('components.credentialReveal.copyFailed', { label: label.toLowerCase() }))
    }
  }

  return (
    <>
      <Space direction="vertical" size={8}>
        <Button onClick={beginReveal} disabled={disabled}>
          {t('components.credentialReveal.reveal')}
        </Button>
        {error && <Alert type="error" showIcon message={error} role="alert" />}
      </Space>

      <Modal
        title={t('components.credentialReveal.confirmTitle')}
        open={confirmationOpen}
        okText={t('components.credentialReveal.confirm')}
        cancelText={t('common.cancel')}
        confirmLoading={loading}
        cancelButtonProps={{ disabled: loading }}
        maskClosable={!loading}
        keyboard={!loading}
        onOk={confirmReveal}
        onCancel={() => {
          if (!loading) {
            setConfirmationOpen(false)
            clearPlaintext()
          }
        }}
        destroyOnHidden
      >
        <Text>
          {t('components.credentialReveal.warning')}
        </Text>
      </Modal>

      <Modal
        title={t('components.credentialReveal.revealedTitle')}
        open={revealedOpen}
        closable={false}
        maskClosable={false}
        keyboard
        onCancel={closeRevealed}
        destroyOnHidden
        footer={
          <Button
            type="primary"
            aria-label={t('components.credentialReveal.closeLabel')}
            onClick={closeRevealed}
          >
            {t('components.credentialReveal.close')}
          </Button>
        }
      >
        {revealed && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Alert
              type="warning"
              showIcon
              message={t('components.credentialReveal.visibleWarning')}
            />
            <Descriptions column={1} size="small">
              <Descriptions.Item label={t('components.credentialReveal.username')}>
                <Space wrap>
                  <Text code style={{ wordBreak: 'break-all' }}>
                    {revealed.username}
                  </Text>
                  <Button
                    size="small"
                    aria-label={t('components.credentialReveal.copyLabel', { label: t('components.credentialReveal.username').toLowerCase() })}
                    onClick={() =>
                      copySecret(t('components.credentialReveal.username'), revealed.username)
                    }
                  >
                    {t('components.credentialReveal.copy')}
                  </Button>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label={t('components.credentialReveal.password')}>
                <Space wrap>
                  <Text code style={{ wordBreak: 'break-all' }}>
                    {revealed.password}
                  </Text>
                  <Button
                    size="small"
                    aria-label={t('components.credentialReveal.copyLabel', { label: t('components.credentialReveal.password').toLowerCase() })}
                    onClick={() =>
                      copySecret(t('components.credentialReveal.password'), revealed.password)
                    }
                  >
                    {t('components.credentialReveal.copy')}
                  </Button>
                </Space>
              </Descriptions.Item>
              {revealed.database_name && (
                <Descriptions.Item label={t('components.credentialReveal.database')}>
                  <Space wrap>
                    <Text code>{revealed.database_name}</Text>
                    <Button
                      size="small"
                      aria-label={t('components.credentialReveal.copyLabel', { label: t('components.credentialReveal.databaseName').toLowerCase() })}
                      onClick={() => {
                        const databaseName = revealed.database_name
                        if (databaseName) {
                          void copySecret(t('components.credentialReveal.databaseName'), databaseName)
                        }
                      }}
                    >
                      {t('components.credentialReveal.copy')}
                    </Button>
                  </Space>
                </Descriptions.Item>
              )}
            </Descriptions>
            {copyStatus && (
              <Text type="secondary" role="status" aria-live="polite">
                {copyStatus}
              </Text>
            )}
          </Space>
        )}
      </Modal>
    </>
  )
}
