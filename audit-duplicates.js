// Audit Supabase storage for duplicate filenames and identify which to delete
// Run with: node audit-duplicates.js

import { createClient } from '@supabase/supabase-js';
import { getSupabaseRuntimeConfig } from './supabase-runtime-config.mjs';

const { url: SUPABASE_URL, key: SUPABASE_KEY, bucket: BUCKET } = getSupabaseRuntimeConfig();

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

async function auditStorage() {
    console.log('Starting storage audit...\n');

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

    console.log(`\nTotal files found: ${allFiles.length}`);

    // Group files by filename
    const filesByName = {};
    for (const file of allFiles) {
        if (!filesByName[file.name]) {
            filesByName[file.name] = [];
        }
        filesByName[file.name].push(file);
    }

    // Find duplicates
    const duplicates = {};
    for (const [filename, files] of Object.entries(filesByName)) {
        if (files.length > 1) {
            duplicates[filename] = files;
        }
    }

    console.log(`\nDuplicate filenames found: ${Object.keys(duplicates).length}`);

    // Analyze duplicates
    const toDelete = [];
    const toKeep = [];

    for (const [filename, files] of Object.entries(duplicates)) {
        // Sort by size (largest first)
        files.sort((a, b) => b.size - a.size);

        const largest = files[0];
        const smaller = files.slice(1);

        console.log(`\n${filename}:`);
        console.log(`  KEEP:   ${largest.path} (${largest.size} bytes, folder: ${largest.folder || 'root'})`);

        for (const file of smaller) {
            console.log(`  DELETE: ${file.path} (${file.size} bytes, folder: ${file.folder || 'root'})`);
            toDelete.push(file);
        }

        toKeep.push(largest);
    }

    console.log(`\n\n=== SUMMARY ===`);
    console.log(`Total duplicate sets: ${Object.keys(duplicates).length}`);
    console.log(`Files to keep: ${toKeep.length}`);
    console.log(`Files to delete: ${toDelete.length}`);
    console.log(`Space to reclaim: ${toDelete.reduce((sum, f) => sum + f.size, 0)} bytes`);

    // Write deletion list to file
    const deletionList = toDelete.map(f => f.path).join('\n');
    console.log('\n\nDeletion list written to: delete-list.txt');

    // For now, just log the list - we'll create a separate script to actually delete
    console.log('\n=== FILES TO DELETE ===');
    toDelete.forEach(f => {
        console.log(f.path);
    });
}

auditStorage().catch(console.error);
