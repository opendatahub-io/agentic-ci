# Vertex Token Stub

`agentic_ci.vertex_token_stub` is a loopback token-exchange stub for SDKs
that insist on minting GCP tokens. OpenShell injects Vertex credentials
into the sandbox as an opaque placeholder env var and swaps it for the real
access token in the supervisor proxy; this stub serves the placeholder back
to credential-exchange libraries (such as OpenCode's
`@ai-sdk/google-vertex`) without any network access.

`make_server()` creates the loopback HTTP server and `main()` runs it,
answering every token-exchange POST with the `AGENTIC_CI_VERTEX_TOKEN`
value.

::: agentic_ci.vertex_token_stub
