---
title: TCC Augusto
emoji: 🧠
colorFrom: indigo
colorTo: purple
sdk: gradio
sdk_version: "5.49.1"
app_file: app.py
pinned: false
---

## Configuração do Vertex AI

O cliente Vertex AI é inicializado automaticamente a partir das
credenciais definidas em variáveis de ambiente. Antes de executar o
aplicativo, garanta que as seguintes variáveis estejam disponíveis no
ambiente (por exemplo, exportadas no shell ou configuradas em um
arquivo `.env`):

```
GCP_PROJECT_ID
GCP_PRIVATE_KEY_ID
GCP_PRIVATE_KEY        # use `\n` para quebras de linha
GCP_CLIENT_EMAIL
GCP_CLIENT_ID
GCP_CLIENT_X509_CERT_URL
```

Também é possível personalizar a região e o modelo padrão através das
variáveis `GOOGLE_CLOUD_LOCATION` e `VERTEX_MODEL`, respectivamente.

O arquivo `requirements.txt` inclui a dependência
`google-cloud-aiplatform`, garantindo que o SDK do Vertex AI seja
instalado junto com o restante das bibliotecas do projeto. Caso deseje
utilizar uma conta de serviço a partir de um arquivo JSON local, basta
exportar as variáveis acima com os valores do arquivo (as chaves
utilizadas em `app/config.py` correspondem aos campos do JSON da conta de
serviço).
