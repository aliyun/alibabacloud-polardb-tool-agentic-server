import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Descriptions,
  Modal,
  Space,
  Typography,
} from 'antd'

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
      setError('Could not reveal credential. Try again or review the audit logs.')
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
      setCopyStatus(`${label} copied`)
    } catch {
      setCopyStatus(`Could not copy ${label.toLowerCase()}`)
    }
  }

  return (
    <>
      <Space direction="vertical" size={8}>
        <Button onClick={beginReveal} disabled={disabled}>
          Reveal credential
        </Button>
        {error && <Alert type="error" showIcon message={error} role="alert" />}
      </Space>

      <Modal
        title="Reveal credential?"
        open={confirmationOpen}
        okText="Confirm"
        cancelText="Cancel"
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
          This security-sensitive action is audited. Keep the credential only
          for the current task and close it when finished.
        </Text>
      </Modal>

      <Modal
        title="Revealed credential"
        open={revealedOpen}
        closable={false}
        maskClosable={false}
        keyboard
        onCancel={closeRevealed}
        destroyOnHidden
        footer={
          <Button
            type="primary"
            aria-label="Close revealed credential"
            onClick={closeRevealed}
          >
            Close
          </Button>
        }
      >
        {revealed && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Alert
              type="warning"
              showIcon
              message="Visible until this dialog closes"
            />
            <Descriptions column={1} size="small">
              <Descriptions.Item label="Username">
                <Space wrap>
                  <Text code style={{ wordBreak: 'break-all' }}>
                    {revealed.username}
                  </Text>
                  <Button
                    size="small"
                    aria-label="Copy username"
                    onClick={() =>
                      copySecret('Username', revealed.username)
                    }
                  >
                    Copy
                  </Button>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="Password">
                <Space wrap>
                  <Text code style={{ wordBreak: 'break-all' }}>
                    {revealed.password}
                  </Text>
                  <Button
                    size="small"
                    aria-label="Copy password"
                    onClick={() =>
                      copySecret('Password', revealed.password)
                    }
                  >
                    Copy
                  </Button>
                </Space>
              </Descriptions.Item>
              {revealed.database_name && (
                <Descriptions.Item label="Database">
                  <Space wrap>
                    <Text code>{revealed.database_name}</Text>
                    <Button
                      size="small"
                      aria-label="Copy database name"
                      onClick={() => {
                        const databaseName = revealed.database_name
                        if (databaseName) {
                          void copySecret('Database name', databaseName)
                        }
                      }}
                    >
                      Copy
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
