# GFM pt-BR Translator

Ferramenta em Python que traduz automaticamente, com IA (DeepSeek), os arquivos de localização do mod **Greater Flavor Mod (GFM)** de **Victoria 2** para **português do Brasil**, sem quebrar o formato do jogo.

> **Aviso importante:** este repositório contém **apenas a ferramenta** e a tradução. Ele **não distribui** arquivos do GFM. Veja [Aviso legal](#aviso-legal).

---

## O que ela resolve

Traduzir localização de Victoria 2 com um LLM "puro" costuma quebrar o jogo ou o texto. Os problemas mais comuns, e como a ferramenta lida com eles:

| Problema | Solução |
|---|---|
| A IA traduz ou apaga variáveis do motor (`$COUNTRY$`, `§Y`, `§!`, `\n`, `%d`, `[GetName]`) | **Blindagem de tokens**: troca cada um por uma âncora opaca (`__V0__`), valida que todas voltaram e restaura. |
| `;` dentro do texto quebra as 15 colunas do CSV | **Sanitização** só no campo traduzido: `;` vira `,`, aspas e travessões tipográficos viram ASCII. |
| Traduções que travam o jogo (`%s`/`%d` fora de ordem, `$VAR$` alterada, `[` `]` quebrados) | **Verificação anti-crash** antes de aceitar cada tradução. |
| Perder dinheiro refazendo tudo a cada atualização do mod | **Cache por hash do texto em inglês**: só o que é novo ou mudou é traduzido. Salvo a cada lote, então dá para pausar e retomar. |
| Codificação Windows-1252 com bytes indefinidos e caracteres fora dela (`ń`, `ő`, `č`) | Leitura/escrita em cp1252 que preserva os 5 bytes indefinidos e **transliteração** do que não existe na codificação. |
| Nomes de países com "O/A/Os/As" no mapa e nas listas (`O Reino Unido`) | Regra no prompt **e** limpeza automática para nomes próprios curtos. |
| Textos que estouram as caixinhas da interface | Tabela de **encaixes de UI** (ex.: "Voting System" → "Sist. Eleitoral"). |

---

## Como funciona

```
CSV original (cp1252)
      │
      ▼
 separa comentários / linhas vazias  ──────────────────────────────┐
      │                                                              │
      ▼                                                              │
 hash SHA-1 do inglês ──► já no cache? ──sim──► usa a tradução salva │
      │ não                                                          │
      ▼                                                              │
 protege tokens ($VAR$, §Y, \n, %d, [..]) → __V0__, __V1__ ...       │
      │                                                              │
      ▼                                                              │
 lotes de 25 → DeepSeek (JSON, thinking desligado)                   │
      │                                                              │
      ▼                                                              │
 valida: âncoras · anti-crash · proporção de tamanho                 │
   └─ falhou? retenta com feedback → depois 1 a 1 → senão fica em    │
      inglês e vai para gfm_falhas.log                               │
      │                                                              │
      ▼                                                              │
 restaura tokens → sanitiza → grava no cache                         │
      │                                                              │
      ▼                                                              ▼
 pós-processa (tira artigo de nomes, encaixes de UI) ──► CSV de saída (15 colunas, cp1252, CRLF)
```

Detalhes de formato: a coluna do inglês (índice 1) é **sobrescrita** com o português; as demais colunas, os comentários e a estrutura de 15 colunas terminada em `x` são preservados.

---

## Requisitos

- **Python 3.10+**
- Uma chave de API do [DeepSeek](https://platform.deepseek.com) (com saldo)
- Victoria 2 + Greater Flavor Mod instalados

## Instalação

```bash
git clone https://github.com/SEU_USUARIO/gfm-ptbr-translator.git
cd gfm-ptbr-translator

python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
```

## Configuração

Copie o arquivo de exemplo e preencha:

```bash
copy .env.example .env      # Windows
# cp .env.example .env      # Linux/macOS
```

| Variável | Obrigatória | Descrição |
|---|---|---|
| `DEEPSEEK_API_KEY` | sim | Chave da API do DeepSeek |
| `GFM_SOURCE_DIR` | sim | Pasta `localisation` do GFM **original** (somente leitura) |
| `GFM_OUTPUT_DIR` | sim | Pasta `localisation` do **mod de tradução** (saída) |
| `GFM_WORK_DIR` | não | Onde ficam o cache e o log de falhas (padrão: pasta atual) |
| `GFM_OUTPUT_PREFIX` | não | Prefixo dos arquivos de saída, ex.: `z_gfm_` (padrão: vazio) |
| `DEEPSEEK_MODEL` | não | Modelo (padrão `deepseek-chat`; se a API recusar, use `deepseek-flash`) |

O `.env` está no `.gitignore`. **Nunca** o publique: ele contém a sua chave.

---

## Uso

```bash
# 1. Ver quantos textos faltam traduzir (não chama a API, não gasta nada)
python gfm_translate_deepseek.py --dry-run

# 2. Testar com um arquivo pequeno antes de rodar tudo
python gfm_translate_deepseek.py --only text.csv

# 3. Traduzir tudo (só o delta, o resto vem do cache)
python gfm_translate_deepseek.py
```

| Opção | O que faz |
|---|---|
| `--dry-run` | Só conta os textos pendentes; não chama a API |
| `--only NOME` | `--only text.csv` processa exatamente esse arquivo; `--only events` processa todos cujo nome contém "events" |
| `--clean-cache` | Remove do cache (dos arquivos selecionados) traduções que violam o anti-crash, textos curtos suspeitos e proporções de tamanho inválidas, e retraduz. Faz backup antes. |
| `--retranslate-names` | Retraduz, com a regra de "sem artigo" do prompt, nomes próprios que ficaram no cache com `O/A/Os/As`. Faz backup antes. |

`Ctrl+C` é seguro: o cache é gravado a cada lote (com troca atômica do arquivo), então é só rodar de novo para continuar.

### Custo

Os preços do DeepSeek mudam; confira a [tabela oficial](https://api-docs.deepseek.com/quick_start/pricing). Como ordem de grandeza, cada texto consome algumas dezenas de tokens de entrada e de saída, o que dá uma fração de centavo de dólar por texto. Use `--dry-run` para saber quantos textos únicos serão enviados e teste um arquivo com `--only` para medir o custo real antes de rodar tudo.

---

## Instalando a tradução no jogo

1. A saída (`GFM_OUTPUT_DIR`) deve estar dentro de uma pasta de mod própria, por exemplo `Victoria 2/mod/GFM Translation/localisation`.
2. Crie um arquivo `.mod` na pasta `Victoria 2/mod/` apontando para ela. Exemplo (ajuste nomes e caminhos; confira a sintaxe aceita pela sua versão do jogo/launcher):

   ```
   name = "GFM Translation"
   path = "mod/GFM Translation"
   dependencies = { "Greater Flavor Mod" }
   ```

3. No launcher, ative o GFM e a tradução, **com a tradução carregando depois do GFM**. Como a tradução usa as mesmas chaves, ela precisa vencer os arquivos originais. Se não vencer, use `GFM_OUTPUT_PREFIX=z_gfm_` para os arquivos carregarem depois (ordem alfabética).
4. O jogo precisa estar no idioma **inglês**, já que a tradução substitui a coluna do inglês.

---

## Verificando o resultado sem abrir o jogo

```bash
python diagnostico_artigos.py
```

Varre os CSVs de saída, acha textos curtos que ainda começam com `O/A/Os/As` e mostra, para cada um, a chave, o inglês de origem, o texto atual e o que a limpeza decidiria. Útil para ajustar a regra de nomes próprios.

Os testes automáticos (não usam a API) cobrem proteção de tokens, anti-crash, encoding, remoção de artigos e o pipeline completo com uma API simulada:

```bash
pip install -r requirements-dev.txt
pytest
```

---

## Personalização

Tudo fica no topo de `gfm_translate_deepseek.py`:

- `GLOSSARY`: termos fixos do jogo (Esfera de Influência, Grande Potência, ...). Vai no prompt e a IA flexiona gênero e número.
- `TARGET_LANG`: tom e idioma de destino. Dá para adaptar para outros idiomas ajustando também o glossário e as regras do prompt.
- `UI_EXACT_FITS` e `UI_PREFIX_FITS`: encurtam textos que estouram caixas pequenas da interface.
- `STRIP_NAME_ARTICLES`, `NAME_MAX_WORDS`: controlam a remoção de artigo em nomes próprios (`False` desliga).
- `BATCH_SIZE`, `TEMPERATURE`, `MAX_RETRIES`: parâmetros de chamada.

---

## Solução de problemas

| Sintoma | O que fazer |
|---|---|
| `Variável GFM_SOURCE_DIR não definida` | Crie o `.env` a partir do `.env.example` e preencha os caminhos |
| `Defina DEEPSEEK_API_KEY` | Falta a chave no `.env` |
| Erro de modelo inexistente | Defina `DEEPSEEK_MODEL=deepseek-flash` |
| Linhas ficaram em inglês | Veja `gfm_falhas.log`: cada entrada traz o motivo e a resposta do modelo. Rode o script de novo: só as falhas são tentadas outra vez |
| Aviso `fora do cp1252` | Caracteres que o jogo não suporta foram transliterados (`ń` → `n`); confira os arquivos citados |
| `[ANTI-CRASH]` no terminal | A barreira final manteve o inglês em uma linha por segurança |
| Nomes ainda com `O/A` | Rode `python diagnostico_artigos.py` e depois `--retranslate-names` |

---

## Aviso legal

- Este projeto **não é afiliado** à Paradox Interactive.
- O texto do GFM pertence aos seus autores.
- A tradução é feita por IA e pode conter erros, inconsistências de nomenclatura e problemas de encaixe na interface. Atualizações, correções e ajustes serão feitas no decorrer dos dias.
- O código desta ferramenta é licenciado sob a [licença MIT](LICENSE).

## Estrutura do repositório

```
.
├── gfm_translate_deepseek.py   # ferramenta principal
├── tests/                      # testes (pytest), sem chamadas à API
├── .env.example                # modelo de configuração
├── requirements.txt            # dependências
├── requirements-dev.txt        # dependências de desenvolvimento (pytest)
├── .gitignore
├── .gitattributes
├── LICENSE
└── README.md
```
