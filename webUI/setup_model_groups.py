"""Keep model groups usable when the wizard removes DeepSeek models."""


def siliconflow_free_text_ids(template):
    """Use the template's free text models, excluding embedding/vision/ASR."""
    models = {model.get("name"): model for model in template.get("models", [])}
    identifiers = []
    for group in ("tool_use", "planner", "utils_small", "utils"):
        for name in template.get("model_task_config", {}).get(group, {}).get("model_list", []):
            model = models.get(name, {})
            identifier = model.get("model_identifier")
            if (str(model.get("api_provider", "")).strip().casefold() == "siliconflow"
                    and model.get("price_in") == 0 and model.get("price_out") == 0
                    and identifier and identifier not in identifiers):
                identifiers.append(identifier)
    return identifiers


def reconcile_deepseek_groups(groups, original_models, models, providers, free_ids):
    """Replace only missing DeepSeek references with available free text models."""
    provider_names = {str(provider.get("name", "")).strip().casefold() for provider in providers}
    deepseek_names = {
        model.get("model_name") or model.get("name") or model.get("model_identifier")
        for model in original_models
        if str(model.get("api_provider", "")).strip().casefold() == "deepseek"
    }
    available = {
        model.get("model_name") or model.get("name") or model.get("model_identifier"): model
        for model in models
        if str(model.get("api_provider", "")).strip().casefold() in provider_names
    }
    fallbacks = [
        name for identifier in free_ids for name, model in available.items()
        if str(model.get("api_provider", "")).strip().casefold() == "siliconflow"
        and model.get("model_identifier") == identifier
    ]
    result = {}
    for group, names in groups.items():
        missing = [name for name in names if name in deepseek_names and name not in available]
        if not missing:
            result[group] = list(names)
            continue
        if not fallbacks:
            raise ValueError("删除 DeepSeek 模型后，请至少保留一个 SiliconFlow 免费文本模型")
        result[group] = list(dict.fromkeys(
            candidate for name in names for candidate in (fallbacks if name in missing else [name])
        ))
    return result
