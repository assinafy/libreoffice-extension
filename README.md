# Assinafy para LibreOffice

Extensão nativa para preparar e acompanhar assinaturas de documentos do Writer,
Calc, Impress e Draw. O documento aberto é exportado para PDF, revisado localmente
e enviado à Assinafy após confirmação.

## Instalação

1. Instale o [LibreOffice estável](https://www.libreoffice.org/download/), versão
   26.8 ou posterior, com Python/UNO 3.11+. Em distribuições Linux que separam
   esses componentes, instale também o provedor de scripts Python do LibreOffice.
2. Abra **Ferramentas → Gerenciador de extensões → Adicionar** e selecione
   o arquivo `assinafy-1.0.1.oxt` baixado da distribuição Assinafy.
3. Reinicie o LibreOffice. O menu **Assinafy** aparece nos quatro editores.
4. Abra **Assinafy → Conectar conta** e clique em **Conectar**.

O pacote contém o SDK Assinafy e as dependências de rede. O uso normal da extensão
não exige instalar pacotes Python separadamente.

A distribuição usa produção por padrão, com o identificador público da aplicação
e a URL HTTPS de retorno incorporados. O usuário final apenas conecta sua conta.

Para atualizar, adicione o novo `.oxt` no gerenciador e reinicie o aplicativo.
Para remover, desconecte a conta e remova a extensão pelo mesmo gerenciador. Documentos enviados permanecem na Assinafy.

## Fluxo do documento

```mermaid
flowchart LR
    A[Documento aberto] --> B[Signatários e ordem]
    B --> C[Exportar e revisar PDF]
    C --> D[Confirmar upload]
    D --> E[Processar PDF e consultar custo]
    E --> F[Confirmar convites]
    F --> G[Assinaturas na Assinafy]
    G --> H[Acompanhar e baixar artefatos]
```

### 1. Conectar a conta

**Conectar** abre o navegador padrão para login e consentimento na Assinafy.
Escolha o workspace que a extensão poderá acessar. A conexão deve autorizar
exatamente um workspace. A extensão não recebe a senha da conta.

As permissões solicitadas são `account:read`, `documents:read`,
`documents:write` e `offline_access`. A falta de permissão de escrita impede
upload, envio, reenvio e exclusão.

Para manter a conexão entre sessões, configure uma senha mestra no gerenciador
de senhas do LibreOffice, nas opções de segurança. No macOS, as opções ficam em
**LibreOffice → Settings/Preferências**. Sem uma senha mestra própria, a extensão
mantém as credenciais apenas na sessão. **Desconectar** revoga a autorização e
remove as credenciais locais.

### 2. Preparar os signatários

Abra o documento e selecione **Assinafy → Enviar para assinatura**.
Adicione o nome completo e o e-mail de cada signatário. Escolha a verificação:

| Método | Dados e comportamento |
| --- | --- |
| `Email` | O signatário recebe um convite por e-mail. |
| `DigitalCertificate` | Exige CPF/CNPJ; a assinatura com certificado ocorre no fluxo da Assinafy. |

Signatários na mesma etapa assinam em paralelo. Etapas diferentes definem a
ordem. Numere as etapas a partir de 1, sem pular números. Um signatário com
certificado deve estar sozinho em sua etapa. O mesmo
e-mail não pode aparecer duas vezes no envio.

Adicione uma mensagem opcional e, se necessário, uma expiração ISO 8601 com fuso
horário, por exemplo `2030-12-31T23:59:00Z`. Contatos existentes são reutilizados
quando o nome e o e-mail correspondem; divergências de nome devem ser corrigidas
na Assinafy antes do envio.

### 3. Revisar o PDF

A extensão exporta o estado atual do documento, incluindo alterações ainda não
salvas. A exportação usa o filtro PDF nativo de cada editor e preserva o arquivo
original e seu local de gravação.

Confira todas as páginas na prévia e clique em **PDF revisado**. No Calc, confira
as áreas de impressão e as quebras de página antes de continuar. O limite de
upload é 25 MiB e 2.000 páginas. Arquivos temporários da prévia são removidos ao
terminar essa etapa.

### 4. Confirmar upload e convites

A primeira confirmação mostra o workspace, o arquivo e os signatários. Aceitá-la
envia o PDF e consulta o custo, sem criar os convites.

A segunda confirmação mostra destinatários, métodos, ordem, mensagem, expiração
e créditos estimados. Somente a confirmação dessa etapa cria o pedido de
assinatura e envia as notificações. A API verifica os recursos disponíveis no
momento da operação; a estimativa não reserva saldo.

Se você cancelar após o upload, o documento permanece preparado na Assinafy.
Abra **Envios recentes → Acompanhar → Retomar envio** para revisar o PDF original e
definir os signatários novamente, sem repetir o upload.

### 5. Acompanhar e recuperar

Em **Assinafy → Envios recentes**, pesquise, navegue pelas páginas e selecione
**Acompanhar**. **Atualizar** consulta o status do documento e de cada signatário.
**Reenviar convite** consulta o custo e pede confirmação para o destinatário
selecionado.

Depois de uma falha ou timeout, consulte o status antes de repetir o envio. Uma
resposta perdida pode ter ocorrido depois de a Assinafy aceitar a operação.
A extensão conserva o identificador conhecido do documento e não repete
automaticamente operações após falhas de rede. Se o upload terminar no servidor
sem retornar um identificador, localize o documento pela lista antes de tentar
novamente.

### 6. Baixar os resultados

Escolha o artefato em **Acompanhar → Baixar**:

| Artefato | Conteúdo |
| --- | --- |
| `original` | PDF enviado. |
| `certificated` | PDF certificado pela Assinafy. |
| `certificate-page` | Página de certificação. |
| `pades` | PDF com assinaturas ICP-Brasil; disponível quando o documento possui esse tipo de assinatura. |
| `bundle` | ZIP dos artefatos disponíveis. |

A disponibilidade depende do processamento e do tipo de assinatura. Escolha um
novo nome de arquivo: a extensão preserva arquivos locais já existentes.

**Excluir** remove o documento remoto somente quando o status retornado pela API
permite a operação. A ação exige confirmação. Baixe os arquivos necessários
antes de excluir. A extensão não apresenta a exclusão como cancelamento que
preserva assinaturas.

## Conexão de produção

A extensão já inclui a configuração pública da aplicação Assinafy. Não é
necessário criar um aplicativo OAuth nem informar credenciais de integração.

| Serviço | Endereço |
| --- | --- |
| Login e consentimento | `https://auth.assinafy.com.br` |
| API | `https://api.assinafy.com.br/v1` |
| Retorno ao LibreOffice | `https://integrations.assinafy.com.br/libreoffice/oauth-callback` |

Após autorizar no navegador, a página de retorno encaminha a conexão ao
LibreOffice neste computador. Mantenha o LibreOffice aberto durante esse
processo. Se a conexão expirar, inicie novamente pelo menu **Conectar conta**.

A extensão solicita acesso ao workspace, leitura e envio de documentos e
renovação da conexão. Ela não solicita acesso a templates. Os documentos são
enviados somente após a confirmação do upload; os convites dependem de uma
segunda confirmação. O login e o consentimento acontecem no site da Assinafy.

## Solução de problemas

| Situação | Ação |
| --- | --- |
| Menu ausente | Confira se a extensão está habilitada, se o provedor Python está instalado e reinicie o LibreOffice. |
| Configuração indisponível | Reinstale o pacote oficial da extensão. |
| Não foi possível concluir a conexão | Mantenha o LibreOffice aberto, confira o acesso aos endereços acima e inicie uma nova conexão. |
| Conexão não permanece após fechar | Configure uma senha mestra própria no armazenamento de senhas do LibreOffice. |
| Recursos insuficientes | Confira o plano e o saldo do workspace na Assinafy; retome o documento preparado. |
| PDF com paginação inesperada | Ajuste impressão/layout no editor e gere outra prévia antes de enviar. |
| Timeout de envio | Atualize o status e confirme se o pedido foi criado antes de repetir. |

## Referências

- [API pública Assinafy](https://api.assinafy.com.br/v1/docs)
- [LibreOffice](https://www.libreoffice.org/)

Licença MIT. As dependências conservam suas próprias licenças no pacote.
