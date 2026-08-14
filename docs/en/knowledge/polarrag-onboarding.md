# PolarRAG onboarding for administrators

[简体中文](../../zh-cn/knowledge/polarrag-onboarding.md)

This guide takes an administrator from a ready PAS installation to a working PolarRAG MCP connection for one user. It uses the native `polarrag` identity provider, covers `PUBLIC` and `PERSONAL` knowledge bases, and enables managed document upload through OSS.

For the API and authorization contract, see [PolarRAG MCP integration](polarrag-mcp.md). For the complete PAS installation procedure, use the repository's `deploy-polardb-agentic-server` Skill.

## Outcome and prerequisites

At the end, the user can connect an MCP client with a `pas_user_agent_` token, discover authorized knowledge resources, search or fetch documents, and upload a local document through an approved upload client.

This guide requires the current upload contract: the MCP catalog exposes
`prepare_document_upload` and `complete_document_upload`, and completion takes
only `upload_session_id`. Do not use it with PAS `v0.0.7`: that release exposes
the legacy `prepare_document_upload`, `resume_document_upload`,
`complete_document_upload`, and `abort_document_upload` tools, and completion
also requires `parts`. Use the `v0.0.7` checkout's
`docs/en/knowledge/polarrag-mcp.md` instead. Before following this guide, use
an immutable PAS revision that contains this file and verify the MCP catalog
matches the current contract.

| PAS revision | MCP upload tools | Completion arguments | Use this guide? |
| --- | --- | --- | --- |
| Legacy `v0.0.7` | `prepare_document_upload`, `resume_document_upload`, `complete_document_upload`, `abort_document_upload` | `upload_session_id`, `parts` | No. Use the checkout's `docs/en/knowledge/polarrag-mcp.md`. |
| `v0.0.8` or a later immutable revision containing this file | `prepare_document_upload`, `complete_document_upload` | `upload_session_id` | Yes. |

You need:

- A deployed PAS instance and console administrator access.
- A reachable PolarRAG/OpenSearch endpoint and an account that can call the required PolarRAG catalog and document APIs.
- A PolarRAG Space with synchronized knowledge bases.
- An OSS bucket registered in the PolarRAG trusted catalog, plus credentials scoped to the required bucket and prefix.
- A stable `PAS_ENCRYPTION_KEY`. PAS uses it to encrypt stored upstream and OSS credentials.

Keep all passwords, AccessKey secrets, bootstrap tokens, and Agent Tokens out of tickets, chat, shell history, and screenshots. Enter them directly in the PAS console or the intended client.

## 1. Deploy and finish PAS setup

Deploy PAS with the `deploy-polardb-agentic-server` Skill. Read the bootstrap token from the protected file on the target host, use it once in the initial setup page, and create the administrator account.

Before continuing, request `/readyz` and require HTTP 200 with both:

- `mode` equal to `READY`.
- `config_status` equal to `CURRENT`.

An open console or a running container alone is not sufficient. If PAS remains in `SETUP`, finish initialization. If `config_status` is not `CURRENT`, apply the required PAS configuration or migration before registering PolarRAG.

## 2. Register PolarRAG and enable a Space

In **Administration > Instances**, choose **Register Instance > PolarRAG** and enter:

- A display name.
- The endpoint scheme, host, and port separately.
- The OpenSearch account and password.
- TLS verification settings appropriate for the endpoint certificate.

Do not embed credentials in the endpoint URL. After registration, require the instance to be active. Open its **Spaces** drawer, enable the intended Space, and synchronize its catalog.

PAS validates the upstream routes it needs. If the instance or Space reports `capability_missing`, stop the delivery and give the PolarRAG operator the missing capability name. Do not work around the failure by broadening PAS authorization. The detailed [integration reference](polarrag-mcp.md) lists the required catalog, document, and upload capabilities.

## 3. Create a PAS user and choose an enterprise identity

In **Administration > Users**, create a built-in user and give the initial password to that user through an approved secret channel. The user record has an immutable external ID; it is the authoritative native identity key even if the username is later changed.

Open **Enterprise Identity** for that user. Choose the Provider that matches the principals in the intended knowledge resource's upstream ACL, and use the identity domain reported by the enabled Space for every mapping:

- `polarrag`: choose the basic native provider when the user has no Feishu or SharePoint identity to match. It accepts only principal type `user`, and the Principal ID is the user's immutable PAS external ID; the console locks this value.
- `feishu`: choose this when the user needs to discover or read Feishu-synchronized documents, or a resource ACL contains a Feishu user or group. Choose `user` or `group` and enter the exact Feishu principal ID used by that ACL.
- `sharepoint`: choose this when the user needs to discover or read SharePoint-synchronized documents, or a resource ACL contains a SharePoint user or group. Choose `user` or `group` and enter the exact SharePoint principal ID used by that ACL.

Add more than one active mapping to the same PAS user when that user needs resources protected by more than one provider. Each mapping establishes identity-domain membership; it does not by itself grant every knowledge base in the Space. Selecting an external provider does not import identities or documents. Until Feishu and SharePoint automatic identity synchronization is released in a forthcoming version, maintain these external mappings manually.

## 4. Expose PUBLIC and PERSONAL knowledge bases

After synchronization, handle each PolarRAG access model explicitly:

- `PUBLIC`: an active resource is discoverable to a mapped user only when the Agent binding also permits it. The Agent can allow all public resources, selected public resources, or none. This scope can only narrow upstream access.
- `PERSONAL`: open the Space's unclaimed personal resources, select the mapped PAS user, then choose **Assign owner and activate**. Only that owner can discover and use the resource. Administrator status does not bypass ownership.

Never put `PERSONAL` resource IDs into the Agent's public-scope selector. To transfer ownership, use the explicit administrator workflow and review the corresponding audit event.

## 5. Configure OSS upload access

Open the enabled Space and choose **Configure OSS**. The bucket and endpoint come from the trusted PolarRAG catalog and are read-only. Enter an OSS AccessKey ID and secret with least privilege on the displayed bucket and configured prefix.

PAS performs a write-and-delete credential probe. The policy must also permit the multipart operations required for uploads on that prefix. A successful probe means the Space is upload-ready; it does not grant access to other buckets or prefixes. PAS encrypts the credentials with `PAS_ENCRYPTION_KEY` and never returns the secret through its API.

If the probe fails, fix the exact OSS endpoint, bucket, prefix, or policy error. Do not disable the probe or replace the trusted catalog values with arbitrary destinations.

## 6. Create the Agent and assign the user

In **Administration > Agents**, create an Agent. Its machine token begins with `pas_agent_`; store it if another PAS automation needs it, but do not use it for PolarRAG because machine tokens do not receive PolarRAG tools.

Open the Agent's PolarRAG settings and:

1. Bind the registered PolarRAG instance.
2. Configure the allowed `PUBLIC` scope: all, selected resources, or none.
3. Assign the PAS user created above.

The user assignment, native principal, active Space, resource ACL, and Agent binding must all agree. The Agent binding cannot widen PolarRAG authorization or grant a `PERSONAL` resource to a different owner.

## 7. Issue and connect the user Agent Token

Have the user sign in. Under **My Instances > MCP connections**, the user chooses the assigned Agent and issues a user Agent Token, optionally with an expiration. It begins with `pas_user_agent_`. A built-in user can reveal an existing token after confirming their password; an SSO user receives newly issued or regenerated plaintext once. An administrator can inspect status and force-revoke the token but cannot retrieve its plaintext.

Copy the generated MCP configuration into the intended client. A typical HTTP connection has this shape:

```json
{
  "mcpServers": {
    "pas-polarrag": {
      "type": "http",
      "url": "https://pas.example.com/mcp",
      "headers": {
        "Authorization": "Bearer pas_user_agent_REDACTED"
      }
    }
  }
}
```

Use the URL generated by PAS; reverse-proxy path prefixes may differ. Revoke the token immediately if it is exposed, and issue a replacement rather than attempting to edit it.

## 8. Upload through the Agent

The upload tools coordinate a resumable transfer; they do not read a local path and they never expose OSS credentials.

1. Call `list_knowledge_resources` and select the target opaque `knowledge_resource_id`.
2. The approved local upload client computes file metadata and calls `prepare_document_upload` through the Agent.
3. The client uploads each part directly to the returned short-lived signed URL. Do not attach the PAS token or OSS credentials to those requests.
4. Call `complete_document_upload`. If it reports missing parts, upload only those parts with the refreshed URLs and call complete again.
5. Save the authoritative `doc_id` returned by completion and poll `doc_status` until ingestion succeeds or reports an actionable error.

The PAS managed upload limit is `100 MiB`. A session lasts `24` hours, each normal part is `8 MiB`, and a signed part URL lasts `15` minutes. If the client cannot perform signed multipart PUT requests, use the PAS web upload flow under **My Instances** instead of asking the MCP tool to open a local file.

For a file larger than this PAS limit, use the enterprise knowledge space automatic document upload workflow instead: upload the file to the knowledge base's OSS path and let the service synchronize and ingest it. See [Use an enterprise knowledge space: upload through OSS](https://help.aliyun.com/zh/polardb/polardb-for-mysql/create-and-use-an-enterprise-knowledge-space#upload-oss-section). This is an enterprise knowledge space upload path, not an MCP request to read a local file.

## Available MCP tools

A correctly authorized `pas_user_agent_` connection can expose these PolarRAG tools:

- `list_knowledge_resources`: list the caller's authorized opaque resource handles.
- `kb_search`: search one resource and return ranked document summaries.
- `kb_fetch_context`: fetch grounded context from selected search results.
- `doc_find_by_name`: find documents without exposing an upstream knowledge-base ID.
- `doc_status`: inspect ingestion or processing state.
- `doc_recall`: recall indexed chunks from a document.
- `doc_get_original`: obtain the original document when the upstream capability permits it.
- `doc_delete`: delete a document; use only after an explicit destructive-action confirmation.
- `doc_rechunk`: request reprocessing with supported chunk settings.
- `prepare_document_upload`: create or resume the signed multipart upload session.
- `complete_document_upload`: verify uploaded parts and submit the document for ingestion.

Tool visibility is capability- and authorization-sensitive. Missing management or upload tools usually indicates upstream capability, resource ACL, Agent assignment, or OSS readiness—not a reason to switch to a machine token.

## Acceptance checks and troubleshooting

Accept the delivery only after all checks pass:

- `/readyz` reports `READY` and `CURRENT`.
- The PolarRAG instance is active; the Space is enabled and synchronized without `capability_missing`.
- The user has an active native mapping in the correct identity domain.
- The expected `PUBLIC` resources and only the user's claimed `PERSONAL` resources appear through `list_knowledge_resources`.
- The MCP catalog contains the tools required for the use case, and an end-to-end search or upload succeeds.
- **Audit Logs** contains the administrative registration, identity, ownership, Agent-assignment, and upload events expected for the operation.

Common failures:

- No resources: check the identity domain, principal, enabled Space, resource state, Agent binding, user assignment, and public scope in that order.
- `capability_missing`: upgrade or configure PolarRAG to provide the named route; PAS cannot emulate it.
- Upload unavailable: verify the trusted catalog destination and the OSS credential probe.
- Forbidden personal resource: verify its owner; administrator access is not an ACL bypass.
- `RERANKER_NOT_CONFIGURED`: configure the PolarRAG reranker or use the upstream-supported search mode. Do not hide the error with retries.

## Enterprise identity synchronization availability

The current release supports manually maintained `feishu` and `sharepoint` user or group principal mappings, but it does not call either provider to synchronize identities or perform batch import. Feishu and SharePoint automatic identity synchronization will be released as soon as possible in a forthcoming version and documented when available. Do not describe or configure the current manual mappings as automatic synchronization.
