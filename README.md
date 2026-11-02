# Assinafy para LibreOffice

Extensão nativa para preparar assinaturas e acompanhar contratos no Writer,
Calc, Impress e Draw. O documento aberto é exportado para PDF, revisado
localmente e enviado à Assinafy somente após confirmação.

## Instalação

1. Instale o LibreOffice 26.8 ou posterior com Python/UNO 3.11+. No Linux,
   instale também o provedor de scripts Python quando a distribuição o separar.
2. Abra **Ferramentas → Gerenciador de extensões → Adicionar**, selecione
   `assinafy-1.3.1.oxt` e reinicie o LibreOffice.
3. Abra **Assinafy → Conectar conta** e clique em **Conectar**.

O pacote já contém o SDK Assinafy e suas dependências. A rede deve permitir
HTTPS com TLS 1.2 ou superior para `auth.assinafy.com.br`,
`api.assinafy.com.br` e `integrations.assinafy.com.br`.

Para atualizar, adicione o novo `.oxt` e reinicie. Para remover, desconecte a
conta e remova a extensão; documentos já enviados permanecem na Assinafy.

## Fluxo do documento

```mermaid
flowchart LR
    A[Documento aberto] --> B[Signatários e ordem]
    B --> C[Exportar e revisar PDF]
    C --> D[Confirmar upload]
    D --> E[Processar e consultar custo]
    E --> F[Confirmar convites]
    F --> G[Assinaturas na Assinafy]
    G --> H[Acompanhar e baixar]
```

### 1. Conectar a conta

**Conectar** abre o navegador para login, consentimento e escolha de um único
workspace. A extensão usa OAuth com PKCE e pede `documents:read`,
`documents:write` e `offline_access`; ela nunca recebe sua senha Assinafy.

Para manter a conexão entre sessões, configure uma senha mestra própria no
armazenamento de senhas do LibreOffice. Sem ela, os tokens ficam somente na
sessão. Cada renovação troca o refresh token e reinicia seu prazo de 30 dias;
após 30 dias sem uso, conecte novamente. **Desconectar** revoga a autorização e
remove os tokens locais.

### 2. Preparar os signatários

Abra o documento, escolha **Assinafy → Enviar para assinatura** e adicione nome
completo e e-mail. A extensão oferece estas verificações:

| Método | Requisitos |
| --- | --- |
| `Email` | Código e convite por e-mail. |
| `Whatsapp` | Código e convite por WhatsApp; telefone E.164 e plano pago. |
| `DigitalCertificate` | CPF/CNPJ, recurso no plano e certificado ICP-Brasil A1/A3 do signatário. |
| `IdDocumentFaceMatchBasic` | CPF, recurso no plano, documento, foto e prova de vida no fluxo hospedado. |

`Email` notifica por e-mail e `Whatsapp` por WhatsApp. Certificado e biometria
aceitam um desses canais; cada signatário recebe exatamente um. O convite por
WhatsApp exige telefone com `+` e código do país. O e-mail continua obrigatório
para cadastrar e reutilizar contatos.

Signatários na mesma etapa assinam em paralelo. As etapas começam em 1 e não
podem ter intervalos; um signatário por certificado fica sozinho em sua etapa.
O mesmo e-mail não pode aparecer duas vezes. CPF e CNPJ têm os dígitos
verificados; certificados aceitam CNPJ numérico ou alfanumérico, e biometria
aceita somente CPF.

Mensagem e expiração são opcionais. A expiração usa RFC 3339 com fuso, como
`2030-12-31T23:59:00Z`, e deve estar pelo menos uma hora no futuro. Contatos
existentes são encontrados pelo e-mail: dados de identidade e telefone ausentes
são preenchidos, mas divergências devem ser corrigidas na Assinafy.

### 3. Revisar e enviar

A extensão exporta o estado atual, inclusive alterações ainda não salvas, sem
mudar o arquivo original. Confira todas as páginas na prévia; no Calc, revise
áreas de impressão e quebras. O limite é 25 MiB e 2.000 páginas.

A primeira confirmação mostra workspace, arquivo, destinatários, métodos,
canais, telefones e etapas. Aceitá-la envia o PDF e consulta o custo sem criar
convites. A segunda mostra também mensagem, expiração e créditos; somente ela
cria o pedido de assinatura e envia notificações. A estimativa não reserva
saldo, e a API revalida plano e recursos no envio.

Se você cancelar depois do upload, use **Envios recentes → Acompanhar → Retomar
envio**. A extensão baixa e revisa o PDF original, consulta o custo novamente e
não cria outra cópia.

### 4. Acompanhar e reenviar

Em **Assinafy → Envios recentes**, pesquise, navegue e escolha **Acompanhar**.
**Atualizar** consulta documento e signatários. **Notificações** mostra cada
envio ou falha e a explicação retornada pela API.

**Reenviar convite** atualiza o destinatário, consulta o custo e mostra e-mail,
canal, telefone e créditos antes de pedir confirmação. Uma leitura final compara
e-mail, telefone e canal; qualquer mudança exige nova confirmação. Convites de
etapas futuras aguardam a conclusão da etapa anterior.

Na biometria, `InReview` e `pending_identity_review` exigem decisão no painel
Assinafy. A extensão exibe a situação, mas documento de identidade, selfie,
decisão da revisão e chaves A1/A3 permanecem no fluxo hospedado.

Depois de falha ou timeout, atualize o documento antes de repetir: a resposta
pode ter se perdido após a operação. A extensão não repete escritas ambíguas e
conserva o ID conhecido para recuperação.

### 5. Baixar ou excluir

**Baixar** oferece:

| Artefato | Conteúdo |
| --- | --- |
| `original` | PDF enviado. |
| `certificated` | PDF certificado pela Assinafy. |
| `certificate-page` | Página de certificação. |
| `pades` | PDF com assinaturas ICP-Brasil, quando disponível. |
| `bundle` | ZIP com os artefatos disponíveis. |

A extensão não sobrescreve arquivos locais. **Excluir** exige confirmação e só
funciona quando o status atual é removível. Baixe antes o que precisa guardar.
Em revisão de identidade, excluir cancela o documento e notifica os signatários;
a confirmação informa essa consequência.

## Gestão de contratos

Em **Envios recentes → Acompanhar → Contrato**, cadastre contraparte, CPF/CNPJ,
valor, moeda, datas, renovação automática, prazo de renovação e aviso prévio.
Revise e confirme toda alteração. Campos vazios removem o valor; responsável,
metadados e referências externas que não aparecem no formulário são preservados.

**Assinafy → Gestão de contratos** pesquisa, filtra e acompanha termos. Leituras
continuam disponíveis sem o recurso do plano; escritas dependem dele. Para
aditivo, renovação ou rescisão, abra o documento do instrumento, clique em
**Vincular**, informe o documento principal e os termos alterados. Instrumentos
pendentes podem ser editados ou desvinculados com confirmação; aplicados são
somente leitura.

**Renovar** confirma o período atual e avança o término pelo prazo configurado,
inclusive em contratos expirados. **Encerrar** aceita um motivo e preserva
documento e assinaturas; contratos encerrados não podem ser renovados. Mudanças
concorrentes exigem atualização e nova confirmação. Avisos e transições seguem
o calendário de Brasília e as preferências do responsável na Assinafy.

## Segurança e solução de problemas

Credenciais ficam no armazenamento de senhas do LibreOffice; PDFs temporários
são removidos depois da prévia. A extensão não solicita acesso a templates ou
webhooks e nunca altera endpoints do workspace.

| Situação | Ação |
| --- | --- |
| Menu ausente | Confira a extensão e o provedor Python; reinicie o LibreOffice. |
| Conexão não conclui | Mantenha o LibreOffice aberto, confira a rede e conecte novamente. |
| Conexão não permanece | Configure uma senha mestra própria no LibreOffice. |
| Recursos insuficientes | Confira plano e saldo; retome o documento preparado. |
| PDF inesperado | Ajuste layout, impressão ou quebras e gere outra prévia. |
| Timeout de envio | Atualize o status antes de repetir. |

Detalhes de endpoints, payloads, recuperação, desenvolvimento e webhooks estão
em [docs/INTEGRATION.md](docs/INTEGRATION.md). Consulte também a
[API Assinafy](https://api.assinafy.com.br/v1/docs), o
[SDK Python](https://github.com/assinafy/python-sdk) e o
[LibreOffice](https://www.libreoffice.org/). Licença MIT.
