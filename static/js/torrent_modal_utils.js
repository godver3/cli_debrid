/**
 * Torrent Modal Utilities
 * Helper functions for the modern torrent results modal
 */

/**
 * Extract quality tags from release title using regex patterns
 * @param {string} title - The release title
 * @returns {Array} Array of tag objects with type and value
 */
function extractQualityTags(title) {
    const tags = [];
    
    // Resolution patterns
    const resolutionPatterns = {
        '2160P': /(?:2160p|4K|UHD)/i,
        '1080P': /1080p/i,
        '720P': /720p/i,
        '480P': /480p/i,
    };
    
    // Source patterns
    const sourcePatterns = {
        'REMUX': /REMUX/i,
        'BluRay': /(?:BluRay|Blu-Ray|BDMV|BDRip)/i,
        'WEB-DL': /WEB-?DL/i,
        'WEBRip': /WEBRip/i,
        'HDTV': /HDTV/i,
        'DVD': /DVDRip/i,
    };
    
    // Codec patterns
    const codecPatterns = {
        'x265': /(?:x265|H\.265|HEVC)/i,
        'x264': /(?:x264|H\.264|AVC)/i,
        'AV1': /AV1/i,
        'VP9': /VP9/i,
    };
    
    // Audio patterns
    const audioPatterns = {
        'Atmos': /Atmos/i,
        'TrueHD': /TrueHD/i,
        'DTS-HD MA': /DTS-?HD\.?MA/i,
        'DTS-X': /DTS-?X/i,
        'DTS': /(?<!HD\.)DTS(?!\-?X)/i,
        'DD+': /(?:DD\+|E-?AC3|DDP)/i,
        'DD': /(?<![\+])DD(?![\+])/i,
        'AAC': /AAC/i,
    };
    
    // Audio channel patterns
    const audioChannelPatterns = {
        '7.1': /7\.1/i,
        '5.1': /5\.1/i,
        '2.0': /2\.0/i,
    };
    
    // HDR patterns
    const hdrPatterns = {
        'DV': /(?:DV|DoVi|Dolby\.?Vision)/i,
        'HDR10+': /HDR10\+/i,
        'HDR10': /HDR10(?!\+)/i,
        'HDR': /(?<!10)HDR(?!\+|10)/i,
    };
    
    // Extract resolution
    for (const [value, pattern] of Object.entries(resolutionPatterns)) {
        if (pattern.test(title)) {
            tags.push({ type: 'resolution', value });
            break;
        }
    }
    
    // Extract source
    for (const [value, pattern] of Object.entries(sourcePatterns)) {
        if (pattern.test(title)) {
            tags.push({ type: 'source', value });
            break;
        }
    }
    
    // Extract codec
    for (const [value, pattern] of Object.entries(codecPatterns)) {
        if (pattern.test(title)) {
            tags.push({ type: 'codec', value });
            break;
        }
    }
    
    // Extract audio
    for (const [value, pattern] of Object.entries(audioPatterns)) {
        if (pattern.test(title)) {
            tags.push({ type: 'audio', value });
            break;
        }
    }
    
    // Extract audio channels
    for (const [value, pattern] of Object.entries(audioChannelPatterns)) {
        if (pattern.test(title)) {
            tags.push({ type: 'audio', value });
            break;
        }
    }
    
    // Extract ALL HDR types (don't break after first match)
    for (const [value, pattern] of Object.entries(hdrPatterns)) {
        if (pattern.test(title)) {
            tags.push({ type: 'hdr', value });
            // Don't break - continue checking for other HDR types
        }
    }
    
    // Extract release group - should be last dash-separated segment before extension or at end
    // Handles: -GROUPNAME.mkv, -GROUPNAME mkv, -GROUPNAME, -GROUP NAME (with spaces)
    let releaseGroupMatch = title.match(/-([A-Za-z][A-Za-z0-9\s]+?)[\.\s](?:mkv|mp4|avi)$/i);
    if (!releaseGroupMatch) {
        // Try without extension (end of string)
        releaseGroupMatch = title.match(/-([A-Za-z][A-Za-z0-9\s]+)$/);
    }
    if (releaseGroupMatch) {
        const group = releaseGroupMatch[1].trim();
        // Filter out common quality terms, movie title words, and short codes
        const notGroups = ['mkv', 'mp4', 'avi', 'x264', 'x265', 'hevc', 'avc', 'bluray', 'webdl', 'webrip', 'hdtv', 
                          '1080p', '720p', '2160p', '480p', 'web', 'dl', 'ddp', 'atmos', 'dts', 'truehd', 'hdr', 'dv', 
                          'h264', 'h265', 'ma', 'predator', 'badlands', 'homeland'];
        if (group && group.length >= 3 && !notGroups.includes(group.toLowerCase())) {
            tags.push({ type: 'group', value: group });
        }
    }
    
    // Extract file extension
    const extMatch = title.match(/\.(mkv|mp4|avi)$/i);
    if (extMatch) {
        tags.push({ type: 'extension', value: extMatch[1].toUpperCase() });
    }
    
    return tags;
}

/**
 * Truncate release title based on media type
 * @param {string} title - Full release title
 * @param {string} mediaType - Type of media (movie/episode/season)
 * @param {number} year - Year for movies
 * @param {string} season - Season number for episodes/seasons
 * @param {string} episode - Episode number for episodes
 * @returns {string} Truncated title
 */
function truncateTitle(title, mediaType, year, season, episode) {
    if (!title) return 'N/A';
    
    if (mediaType === 'movie') {
        // Movies: truncate to "Name 2021" or "Name.2021"
        // Match year with optional surrounding spaces/dots/dashes
        const yearMatch = title.match(new RegExp(`^(.+?[\\s\\.\\-])${year}(?:[\\s\\.\\-]|$)`, 'i'));
        if (yearMatch) {
            // Return everything up to and including the year
            const endPos = yearMatch.index + yearMatch[0].length;
            // Trim trailing space/dot/dash if followed by more content
            let result = title.substring(0, endPos);
            result = result.replace(/[\s\.\-]+$/, ''); // Remove trailing separators
            return result + (title[endPos - 1] === ' ' ? ' ' : title[endPos - 1] === '.' ? '.' : '');
        }
        // Fallback: try to find year anywhere and truncate there
        const simpleYearMatch = title.match(new RegExp(`${year}`));
        if (simpleYearMatch) {
            return title.substring(0, simpleYearMatch.index + 4);
        }
    } else if (episode) {
        // Episodes: truncate to "Name.S01E03" or "Name S01E03"
        const seasonEp = `S${String(season).padStart(2, '0')}E${String(episode).padStart(2, '0')}`;
        const epMatch = title.match(new RegExp(`^(.+?[\\s\\.\\-])${seasonEp}(?:[\\s\\.\\-]|$)`, 'i'));
        if (epMatch) {
            const endPos = epMatch.index + epMatch[0].length;
            let result = title.substring(0, endPos);
            result = result.replace(/[\s\.\-]+$/, '');
            return result;
        }
    } else if (season) {
        // Shows/Seasons: "Name S01" or "Name.S01-04" etc.
        // Match various season patterns
        const seasonPatterns = [
            `S${String(season).padStart(2, '0')}-S\\d{2}`,  // S01-S04
            `S${String(season).padStart(2, '0')}-\\d{2}`,    // S01-04
            `S${String(season).padStart(2, '0')}`            // S01
        ];
        
        for (const pattern of seasonPatterns) {
            const seasonMatch = title.match(new RegExp(`^(.+?[\\s\\.\\-])${pattern}(?:[\\s\\.\\-]|$)`, 'i'));
            if (seasonMatch) {
                const endPos = seasonMatch.index + seasonMatch[0].length;
                let result = title.substring(0, endPos);
                result = result.replace(/[\s\.\-]+$/, '');
                return result;
            }
        }
    }
    
    // Fallback: return original title
    return title;
}

/**
 * Generate HTML for quality badge
 * @param {Object} tag - Tag object with type and value
 * @returns {string} HTML string for badge
 */
function createQualityBadge(tag) {
    return `<span class="quality-badge ${tag.type}">${tag.value}</span>`;
}

/**
 * Generate multi-provider cache badge HTML
 * Shows a small provider abbreviation badge for each provider that has it cached.
 * Falls back to single icon when only one provider is configured.
 * @param {string} status - Overall cache status
 * @param {Object} cacheProviders - Per-provider cache status {ProviderName: 'Yes'/'No'/'Error'}
 * @returns {string} HTML string
 */
function createMultiProviderCacheIcon(status, cacheProviders) {
    if (!cacheProviders || Object.keys(cacheProviders).length <= 1) {
        return createCacheIcon(status);
    }
    const abbrev = {
        'Real-Debrid': 'RD', 'AllDebrid': 'AD', 'Torbox': 'TB',
        'Premiumize': 'PM', 'Debrid-Link': 'DL',
    };
    const badges = Object.entries(cacheProviders).map(([name, st]) => {
        const ab = abbrev[name] || name.slice(0, 2).toUpperCase();
        const cached = st === 'Yes';
        const cls = cached ? 'cp-badge cp-cached' : 'cp-badge cp-uncached';
        const title = `${name}: ${st}`;
        return `<span class="${cls}" title="${title}">${ab}</span>`;
    }).join('');
    return `<div class="cache-providers-wrap">${badges}</div>`;
}

/**
 * Generate cache status icon HTML
 * @param {string} status - Cache status (Yes/No/Unknown/etc)
 * @returns {string} HTML string for cache icon
 */
function createCacheIcon(status) {
    const normalized = status.toLowerCase();
    
    if (normalized === 'yes' || normalized === 'cached') {
        return `
            <div class="cache-status-icon cached" title="Cached">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
                    <path fill-rule="evenodd" d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z" clip-rule="evenodd" />
                </svg>
            </div>
        `;
    } else if (normalized === 'no' || normalized === 'not cached') {
        return `
            <div class="cache-status-icon not-cached" title="Not Cached">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
                    <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
                </svg>
            </div>
        `;
    } else {
        return `
            <div class="cache-status-icon unknown" title="${status}">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
                    <path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-8-5a.75.75 0 01.75.75v4.5a.75.75 0 01-1.5 0v-4.5A.75.75 0 0110 5zm0 10a1 1 0 100-2 1 1 0 000 2z" clip-rule="evenodd" />
                </svg>
            </div>
        `;
    }
}

const _CP_ABBREV = {'Real-Debrid':'RD','AllDebrid':'AD','Torbox':'TB','Premiumize':'PM','Debrid-Link':'DL'};
// Known provider priority order — primary first, fallbacks after
const _CP_ORDER = ['Real-Debrid','AllDebrid','Torbox','Premiumize','Debrid-Link'];

function createCacheProviderBadges(torrent) {
    const cpData = torrent.cache_providers || {};
    const keys = Object.keys(cpData);
    if (!keys.length) return null;
    // Sort by known order, unknown providers appended at end
    const sorted = [...keys].sort((a, b) => {
        const ai = _CP_ORDER.indexOf(a), bi = _CP_ORDER.indexOf(b);
        if (ai === -1 && bi === -1) return 0;
        if (ai === -1) return 1;
        if (bi === -1) return -1;
        return ai - bi;
    });
    const badges = sorted.map(k => {
        const v = cpData[k];
        const cls = v === 'Yes' ? 'cp-cached' : v === 'No' ? 'cp-uncached' : v === 'N/A' ? 'cp-na' : 'cp-not-checked';
        const abbrev = _CP_ABBREV[k] || k.slice(0,2).toUpperCase();
        return `<span class="cp-badge ${cls}" title="${k}: ${v}">${abbrev}</span>`;
    }).join('');
    return `<span class="cp-badges">${badges}</span>`;
}

/**
 * Get score color class based on value
 * @param {number} score - The score value
 * @returns {string} Color class name
 */
function getScoreColorClass(score) {
    if (score >= 1000) return 'high';
    if (score >= 500) return 'medium';
    return 'low';
}

/**
 * Create download icon SVG
 * @returns {string} SVG HTML
 */
function createDownloadIcon() {
    return `
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
            <path d="M10.75 2.75a.75.75 0 00-1.5 0v8.614L6.295 8.235a.75.75 0 10-1.09 1.03l4.25 4.5a.75.75 0 001.09 0l4.25-4.5a.75.75 0 00-1.09-1.03l-2.955 3.129V2.75z" />
            <path d="M3.5 12.75a.75.75 0 00-1.5 0v2.5A2.75 2.75 0 004.75 18h10.5A2.75 2.75 0 0018 15.25v-2.5a.75.75 0 00-1.5 0v2.5c0 .69-.56 1.25-1.25 1.25H4.75c-.69 0-1.25-.56-1.25-1.25v-2.5z" />
        </svg>
    `;
}

/**
 * Create external link icon SVG
 * @returns {string} SVG HTML
 */
function createExternalLinkIcon() {
    return `
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
            <path fill-rule="evenodd" d="M4.25 5.5a.75.75 0 00-.75.75v8.5c0 .414.336.75.75.75h8.5a.75.75 0 00.75-.75v-4a.75.75 0 011.5 0v4A2.25 2.25 0 0112.75 17h-8.5A2.25 2.25 0 012 14.75v-8.5A2.25 2.25 0 014.25 4h5a.75.75 0 010 1.5h-5z" clip-rule="evenodd" />
            <path fill-rule="evenodd" d="M6.194 12.753a.75.75 0 001.06.053L16.5 4.44v2.81a.75.75 0 001.5 0v-4.5a.75.75 0 00-.75-.75h-4.5a.75.75 0 000 1.5h2.553l-9.056 8.194a.75.75 0 00-.053 1.06z" clip-rule="evenodd" />
        </svg>
    `;
}

/**
 * Create refresh icon SVG
 * @returns {string} SVG HTML
 */
function createRefreshIcon() {
    return `
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
            <path fill-rule="evenodd" d="M15.312 11.424a5.5 5.5 0 01-9.201 2.466l-.312-.311h2.433a.75.75 0 000-1.5H3.989a.75.75 0 00-.75.75v4.242a.75.75 0 001.5 0v-2.43l.31.31a7 7 0 0011.712-3.138.75.75 0 00-1.449-.39zm1.23-3.723a.75.75 0 00.219-.53V2.929a.75.75 0 00-1.5 0V5.36l-.31-.31A7 7 0 003.239 8.188a.75.75 0 101.448.389A5.5 5.5 0 0113.89 6.11l.311.31h-2.432a.75.75 0 000 1.5h4.243a.75.75 0 00.53-.219z" clip-rule="evenodd" />
        </svg>
    `;
}

/**
 * Create close X icon SVG
 * @returns {string} SVG HTML
 */
function createCloseIcon() {
    return `
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
            <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
        </svg>
    `;
}

/**
 * Create filter/search icon SVG
 * @returns {string} SVG HTML
 */
function createSearchIcon() {
    return `
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
            <path fill-rule="evenodd" d="M9 3.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM2 9a7 7 0 1112.452 4.391l3.328 3.329a.75.75 0 11-1.06 1.06l-3.329-3.328A7 7 0 012 9z" clip-rule="evenodd" />
        </svg>
    `;
}

/**
 * Create folder icon SVG
 * @returns {string} SVG HTML
 */
function createFolderIcon() {
    return `
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
            <path d="M3.75 3A1.75 1.75 0 002 4.75v3.26a3.235 3.235 0 011.75-.51h12.5c.644 0 1.245.188 1.75.51V6.75A1.75 1.75 0 0016.25 5h-4.836a.25.25 0 01-.177-.073L9.823 3.513A1.75 1.75 0 008.586 3H3.75zM3.75 9A1.75 1.75 0 002 10.75v4.5c0 .966.784 1.75 1.75 1.75h12.5A1.75 1.75 0 0018 15.25v-4.5A1.75 1.75 0 0016.25 9H3.75z" />
        </svg>
    `;
}
