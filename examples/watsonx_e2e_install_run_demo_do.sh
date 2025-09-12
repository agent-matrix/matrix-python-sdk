bash examples/watsonx_e2e_install_run_demo.sh \
  --id mcp_server:watsonx-agent@0.1.0 \
  --alias watsonx-chat \
  --hub-base "${MATRIX_HUB_BASE:-https://api.matrixhub.io}" \
  --token   "${MATRIX_HUB_TOKEN:-}"
