"""Whether a parsed release's HDR tags mean HDR (no imports: safe anywhere)."""


def hdr_tags_mean_hdr(hdr_info) -> bool:
    """True when PTT's `hdr` value names an HDR format (HDR, HDR10, HDR10+, DV...).

    PTT also reports a release marked SDR as hdr=['SDR']. A plain truthiness
    check read that as HDR, so "Show.S06E01.480p.NF.WEB-DL.SDR.AV1-GRiMM" was
    rejected as HDR content whenever HDR was disabled."""
    if not hdr_info:
        return False
    tags = [hdr_info] if isinstance(hdr_info, str) else list(hdr_info)
    return any(str(t).strip().upper() not in ('', 'SDR') for t in tags)
