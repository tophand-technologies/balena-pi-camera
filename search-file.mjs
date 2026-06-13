// Search for a specific file in Supabase storage
import { createClient } from '@supabase/supabase-js';
import { getSupabaseRuntimeConfig } from './supabase-runtime-config.mjs';

const { url: SUPABASE_URL, key: SUPABASE_KEY, bucket: BUCKET } = getSupabaseRuntimeConfig();
const SEARCH_FILENAME = 'PICT3821_S_202603090102WL48X.jpg';

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

async function listAllFiles(prefix, path = '') {
    const fullPath = path ? `${prefix}/${path}` : prefix;
    const files = [];

    const { data, error } = await supabase.storage
        .from(BUCKET)
        .list(fullPath, {
            limit: 1000,
            sortBy: { column: 'created_at', order: 'desc' }
        });

    if (error) {
        console.error('Error listing files:', error);
        return files;
    }

    for (const item of data) {
        if (item.id === null) {
            // It's a folder, recurse
            const subPath = path ? `${path}/${item.name}` : item.name;
            const subFiles = await listAllFiles(prefix, subPath);
            files.push(...subFiles);
        } else {
            // It's a file
            const filePath = path ? `${prefix}/${path}/${item.name}` : `${prefix}/${item.name}`;
            files.push({
                name: item.name,
                path: filePath,
                size: item.metadata?.size || 0,
                created_at: item.created_at,
                device: prefix,
                folder: path
            });
        }
    }

    return files;
}

async function searchFile() {
    console.log(`Searching for: ${SEARCH_FILENAME}\n`);

    // Get top-level folders (device names)
    const { data: devices, error } = await supabase.storage
        .from(BUCKET)
        .list('', {
            limit: 1000
        });

    if (error) {
        console.error('Error listing devices:', error);
        return;
    }

    const allFiles = [];
    for (const device of devices) {
        if (device.id === null) { // It's a folder
            console.log(`Scanning device: ${device.name}...`);
            const files = await listAllFiles(device.name);
            allFiles.push(...files);
        }
    }

    // Find all instances of the file
    const matches = allFiles.filter(f => f.name === SEARCH_FILENAME);

    console.log(`\nFound ${matches.length} instances of ${SEARCH_FILENAME}:\n`);

    for (const match of matches) {
        console.log(`Path: ${match.path}`);
        console.log(`Size: ${match.size} bytes (${(match.size / 1024).toFixed(2)} KB)`);
        console.log(`Folder: ${match.folder || 'root'}`);
        console.log(`Device: ${match.device}`);
        console.log(`Created: ${match.created_at}`);
        console.log('---');
    }
}

searchFile().catch(console.error);
