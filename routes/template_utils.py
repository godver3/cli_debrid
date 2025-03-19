from markupsafe import Markup

def render_settings(settings, section):
    html = f"<h3>{section}</h3>"
    for key, value in settings.items():
        if isinstance(value, dict):
            html += render_settings(value, key)
        else:
            html += f"""
            <div class="form-group">
                <label for="{section}-{key}">{key}:</label>
                <input type="text" id="{section}-{key}" name="{section}.{key}" value="{value}">
            </div>
            """
    return Markup(html)

def render_content_sources(settings, parent_key):
    html = f"<h3>{parent_key}</h3>"
    for key, value in settings.items():
        html += f"<h4>{key}</h4>"
        for sub_key, sub_value in value.items():
            if sub_key == 'enabled':
                html += f"""
                <div class="form-group">
                    <label for="{parent_key}.{key}.{sub_key}">{sub_key.replace('_', ' ').title()}:</label>
                    <select id="{parent_key}.{key}.{sub_key}" name="{parent_key}.{key}.{sub_key}">
                        <option value="true" {"selected" if sub_value else ""}>True</option>
                        <option value="false" {"selected" if not sub_value else ""}>False</option>
                    </select>
                </div>
                """
            elif sub_key == 'versions':
                html += f"<h5>Versions</h5>"
                for version, version_enabled in sub_value.items():
                    html += f"""
                    <div class="form-group">
                        <label for="{parent_key}.{key}.versions.{version}">{version}:</label>
                        <select id="{parent_key}.{key}.versions.{version}" name="{parent_key}.{key}.versions.{version}">
                            <option value="true" {"selected" if version_enabled else ""}>True</option>
                            <option value="false" {"selected" if not version_enabled else ""}>False</option>
                        </select>
                    </div>
                    """
            else:
                html += f"""
                <div class="form-group">
                    <label for="{parent_key}.{key}.{sub_key}">{sub_key.replace('_', ' ').title()}:</label>
                    <input type="text" id="{parent_key}.{key}.{sub_key}" name="{parent_key}.{key}.{sub_key}" value="{sub_value}">
                </div>
                """
    return Markup(html)