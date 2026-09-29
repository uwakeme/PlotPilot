"""OpenAI 兼容网关：模型列表请求的 base URL 归一化。"""

from interfaces.api.v1.workbench.llm_control import _openai_compatible_models_base


def test_empty_defaults_to_official_v1():
    assert _openai_compatible_models_base('') == 'https://api.openai.com/v1'


def test_host_only_appends_v1():
    assert _openai_compatible_models_base('https://api.zhongzhuan.win') == 'https://api.zhongzhuan.win/v1'
    assert _openai_compatible_models_base('https://api.zhongzhuan.win/') == 'https://api.zhongzhuan.win/v1'


def test_preserves_non_root_path():
    assert _openai_compatible_models_base('https://ark.cn-beijing.volces.com/api/v3') == (
        'https://ark.cn-beijing.volces.com/api/v3'
    )


def test_explicit_v1_unchanged():
    assert _openai_compatible_models_base('https://x.example/v1') == 'https://x.example/v1'


def test_embeddings_endpoint_suffix_stripped():
    """嵌入页常粘贴完整 embeddings 端点；列表接口应剥掉该后缀回退到版本根。"""
    assert _openai_compatible_models_base('https://api.siliconflow.cn/v1/embeddings') == (
        'https://api.siliconflow.cn/v1'
    )
    assert _openai_compatible_models_base('https://api.siliconflow.cn/v1/embeddings/') == (
        'https://api.siliconflow.cn/v1'
    )


def test_embeddings_only_path_falls_back_to_v1():
    assert _openai_compatible_models_base('https://x.example/embeddings') == 'https://x.example/v1'
