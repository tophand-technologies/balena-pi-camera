// Delete all _S images under 10 KB from Supabase storage
// Run with: node cleanup-tiny-s-images.mjs

import { createClient } from '@supabase/supabase-js';
import { getSupabaseRuntimeConfig } from './supabase-runtime-config.mjs';

const { url: SUPABASE_URL, key: SUPABASE_KEY, bucket: BUCKET } = getSupabaseRuntimeConfig();
const SIZE_THRESHOLD = 10 * 1024; // 10 KB in bytes

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

async function cleanupTinySImages() {
    console.log('🔍 Scanning for tiny _S images (< 10 KB)...\n');

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

    console.log(`\nTotal files scanned: ${allFiles.length}`);

    // Find _S images under 10 KB
    const tinySImages = allFiles.filter(file => {
        return file.name.includes('_S_') && file.size < SIZE_THRESHOLD;
    });

    console.log(`\n📊 Found ${tinySImages.length} tiny _S images to delete:\n`);

    if (tinySImages.length === 0) {
        console.log('✅ No tiny _S images found! Storage is clean.');
        return;
    }

    // Show some examples
    const examples = tinySImages.slice(0, 10);
    console.log('Examples:');
    examples.forEach(file => {
        console.log(`  ${file.path} - ${(file.size / 1024).toFixed(2)} KB`);
    });
    if (tinySImages.length > 10) {
        console.log(`  ... and ${tinySImages.length - 10} more`);
    }

    // Calculate space to reclaim
    const totalSize = tinySImages.reduce((sum, f) => sum + f.size, 0);
    console.log(`\nSpace to reclaim: ${(totalSize / 1024 / 1024).toFixed(2)} MB`);

    console.log('\n⚠️  WARNING: This will permanently delete these files from Supabase storage!');
    console.log('Press Ctrl+C within 5 seconds to cancel...\n');

    await new Promise(resolve => setTimeout(resolve, 5000));

    console.log('Starting deletion...\n');

    let deleted = 0;
    let errors = 0;

    for (const file of tinySImages) {
        try {
            const { error } = await supabase.storage
                .from(BUCKET)
                .remove([file.path]);

            if (error) {
                throw error;
            }

            deleted++;
            if (deleted % 50 === 0) {
                console.log(`Progress: ${deleted}/${tinySImages.length} deleted`);
            }

            // Small delay to avoid rate limiting
            if (deleted % 10 === 0) {
                await new Promise(resolve => setTimeout(resolve, 100));
            }
        } catch (error) {
            errors++;
            console.error(`✗ Error deleting ${file.path}:`, error.message);
        }
    }

    console.log('\n=== DELETION COMPLETE ===');
    console.log(`Successfully deleted: ${deleted} files`);
    console.log(`Errors: ${errors} files`);
    console.log(`Space reclaimed: ${(totalSize / 1024 / 1024).toFixed(2)} MB`);
}

cleanupTinySImages().catch(console.error);
