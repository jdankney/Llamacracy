# Continue.dev example config

This is an example [Continue](https://continue.dev) config for coding with models served from your own
OpenAI-compatible server (llama.cpp, vLLM, LiteLLM and so on). Your API key lives in a separate `.env`
file, so you can share or commit the config without leaking it.

| File | What it is |
| --- | --- |
| `config.yaml` | The Continue config: models, a dedicated apply model, and a rule for safer edits |
| `.env.example` | Template for the file that holds your API key |

## Install

**1. Copy the config** into Continue's global config folder:

```bash
# macOS / Linux
mkdir -p ~/.continue
cp config.yaml ~/.continue/config.yaml
```

```powershell
# Windows (PowerShell)
New-Item -ItemType Directory -Force "$HOME\.continue" | Out-Null
Copy-Item config.yaml "$HOME\.continue\config.yaml"
```

If you already have a `config.yaml` there, back it up first.

**2. Add your API key.** Copy the template and put your real key in it:

```bash
cp .env.example ~/.continue/.env
```

Then edit `~/.continue/.env`:

```
LLM_API_KEY=sk-your-real-key
```

**3. Point it at your server.** In `~/.continue/config.yaml`, change these values:

- `apiBase` under `model_defaults`: your server's `/v1` URL
- each `model:` value: the model IDs your server exposes (`curl <apiBase>/models` lists them)
- `contextLength`: the context size your server actually runs with

**4. Reload.** Restart your editor, or run **Developer: Reload Window** in VS Code. Then pick your
models from the model dropdown in Continue, and choose **My Apply Model** as the Apply model in
Continue's model settings.

## How it works

### One place for connection settings

Normally every model entry repeats `provider`, `apiBase` and `apiKey`. This config defines them once
in a YAML anchor and merges them into each model:

```yaml
model_defaults: &server        # "&server" names this block
  provider: openai
  apiBase: https://your-server.example.com/v1
  apiKey: "${{ secrets.LLM_API_KEY }}"

models:
  - name: My Chat Model
    <<: *server                # "<<: *server" pastes the block in here
    model: your-chat-model-id
```

To change your URL or key, you edit one line. Any key you set on a model overrides the shared value;
the apply model does this with `roles`. The `%YAML 1.1` line at the top of the file is required for
this syntax, so keep it.

The two chat models also share their sampling settings the same way (`&chat_sampling` /
`*chat_sampling`).

### Secrets

`${{ secrets.LLM_API_KEY }}` tells Continue to look up `LLM_API_KEY` in a `.env` file instead of
reading a literal value. Continue checks these locations, and the first ones win:

1. `.env` at your project's root, or `<project>/.continue/.env`
2. `~/.continue/.env` (global)

Because a project's own `.env` takes precedence, the key has a specific name (`LLM_API_KEY`) rather
than a generic one like `API_KEY`, which your projects might already define for something else.

### The apply model

When the chat model suggests a change, Continue sends it to a model with the `apply` role, which
merges it into your file as a clean diff. If no model has the `apply` role, edits get applied badly.
The example uses the same model as chat but with a low temperature, and with thinking turned off
through `chat_template_kwargs`. That keeps reasoning text out of your files. `enable_thinking` is
for Qwen3-style models on llama.cpp or vLLM; remove the `requestOptions` block if your model
doesn't use it.

### The edit rule

In Agent mode, Continue's `edit_existing_file` tool expects the model to send only the changed code,
with `// ... existing code ...` placeholders for everything else. Smaller local models often leave
out the placeholders, and Continue then replaces the whole file with the snippet. The rule under
`rules:` tells the model to use `single_find_and_replace` instead, which only changes the text it
matches.

## Troubleshooting

- **Files get wiped or truncated when edited.** Check that the apply model is selected, and raise its
  `maxTokens` above the size of your largest file. Use git so a bad edit is easy to undo.
- **Tool calls show up as raw text in chat.** Your server isn't parsing tool calls. llama.cpp needs
  `--jinja`; vLLM needs `--enable-auto-tool-choice` plus the right `--tool-call-parser`.
- **401 / unauthorized.** Check that `~/.continue/.env` exists, the variable name matches the config
  exactly, and no project `.env` defines the same name with a different value.
- **The context percentage looks off.** Continue calculates it from `contextLength`, so set that to
  your server's real value. The count is an estimate from a GPT tokenizer, so it won't be exact for
  other models.

## Security

- Never commit `~/.continue/.env` or any file containing a real key.
- Use `https://` for `apiBase` unless the server is on your local network, or your key is sent in
  plain text.
