"""Default virtual folders for the built-in mount: cli_docs' cli_mount
Content routing patterns (TV Shows / Movies). TV Shows takes a release when
its name OR one of its files looks like TV; Movies takes the rest, so every
release lands in exactly one of the two. Keep in sync with
cli_docs/docs/integrations/cli_mount.md#content-routing."""
import json

TV_NAME_REGEX = '(?i)((?:^|[^a-z0-9])S[0-9]{1,4}(?:[ ._-]?E[0-9]{1,4}|E?(?:[^a-z0-9]|$))|(?:^|[^a-z0-9])[0-9]{1,2}E[0-9]{2,3}(?:[^a-z0-9]|$)|(?:^|[^a-z0-9])Ep[ .]?[0-9]{1,4}(?:[^a-z0-9]|$)|(?:^|[^a-z0-9])[0-9]{1,2}of[0-9]{1,2}(?:[^a-z0-9]|$)|Series[ ._-]*[0-9]{1,2}(?:[^a-z0-9]|$)|Cap[ií]tulo[ ._-]*[0-9]{1,4}|(?:^|[^a-z0-9])EP?[0-9]{2,4}(?:[^a-z0-9]|$)|(?:^|[^a-z0-9])[0-9]{1,2}x[0-9]{2,3}(?:[^a-z0-9]|$)|(?:Seasons?|Temporadas?|Saison|Staffel|Sezon|Stagione|Seizoen|Сезон)[ ._-]*[0-9]{1,2}(?:[^a-z0-9]|$)|[0-9]{1,2}(?:st|nd|rd|th|ª|º)?[ ._-]*(?:Seasons?|Temporadas?|Saison|Staffel|Sezon|Stagione|Seizoen|Сезон)(?:[^a-z0-9]|$)|[EÉ]pisod(?:e|io)s?[ ._-]*[0-9]{1,4}|(?:Complete|Mini|TV|Web)[ ._-]?Series|\\[Cap[ .][0-9]{3,4}\\]|[\\[(][0-9]{2,3} ?[~-] ?[0-9]{2,4}[\\])]|[0-9]{2,4} ?~ ?[0-9]{2,4}|[ _]-[ _](?:[0-9]{2,3}|1[01][0-79][0-9]|108[1-9])(?:v[0-9])?(?:[ .](?:[^0-9]|$)|[_\\[(\\]~-]|$)|(?:^|[^a-z0-9])(?:19|20)[0-9]{2}[ ._-][0-9]{2}[ ._-][0-9]{2}(?:[^a-z0-9]|$))'

TV_FILES_REGEX = '(?i)((?:^|[^a-z0-9])s[0-9]{1,4}[ ._-]*e[0-9]{1,4}|(?:^|[^a-z0-9])s[0-9]{2,3}p[0-9]{2}|(?:^|[^a-z0-9])[0-9]{1,2}x[0-9]{2,3}(?:[^a-z0-9]|$)|(?:^|[^a-z0-9])ep?[0-9]{2,4}(?:[^a-z0-9]|$)|[eé]pisod(?:e|io)s?[ ._-]*[0-9]{1,4}|(?:^|[^a-z0-9])[0-9]{1,2}of[0-9]{1,2}(?:[^a-z0-9]|$)|[ _]-[ _](?:[0-9]{2,3}|1[01][0-79][0-9]|108[1-9])(?:v[0-9])?[ _\\[(]|(?:^|[^a-z0-9])(?:19|20)[0-9]{2}[ ._-][0-9]{2}[ ._-][0-9]{2}(?:[^a-z0-9]|$)|(?:^|[^a-z0-9])(?:19|20)[0-9]{2}[ .][0-9]{3}[ .])'

DEFAULT_VIRTUAL_FOLDERS = [
    {'name': 'TV Shows', 'filters': [
        {'type': 'regex', 'value': TV_NAME_REGEX},
        {'type': 'files_regex', 'value': TV_FILES_REGEX},
    ]},
    {'name': 'Movies', 'filters': [
        {'type': 'not_regex', 'value': TV_NAME_REGEX},
        {'type': 'not_files_regex', 'value': TV_FILES_REGEX},
    ]},
]

DEFAULT_VIRTUAL_FOLDERS_JSON = json.dumps(DEFAULT_VIRTUAL_FOLDERS)
