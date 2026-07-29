"""
SpliceBERT plugin support package.

Unlike SpliceFormer (`pipeline/models/spliceformer/`), no source code
is vendored here: SpliceBERT's official checkpoints are plain
HuggingFace `BertForMaskedLM` weights (`architectures: ["BertForMaskedLM"]`
in the checkpoint's own `config.json`, confirmed by inspecting the
downloaded checkpoint directly) with a standard `BertTokenizer`
vocabulary -- both loadable with `transformers`' own
`AutoModelForMaskedLM` / `AutoTokenizer`, no custom `nn.Module` to
copy in. `loader.py` only has to fetch and unpack the official weight
archive; the model class itself comes from the `transformers` package
GEPER already depends on.

The actual `PluginModel` subclass (`SpliceBERTPlugin`) lives at
`pipeline/models/splicebert_plugin.py`, one level up -- matching where
`SpliceFormerPlugin`/`EnformerPlugin`/`BorzoiPlugin` live.
"""
