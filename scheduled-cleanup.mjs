#!/usr/bin/env node
// Scheduled cleanup script for tiny _S images
// Run this with cron or Windows Task Scheduler every hour:
// Windows: schtasks /create /tn "Cleanup Tiny S Images" /tr "node C:\path\to\scheduled-cleanup.mjs" /sc hourly

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
            const subPath = path ? `${path}/${item.name}` : item.name;
            const subFiles = await listAllFiles(prefix, subPath);
            files.push(...subFiles);
        } else {
            const filePath = path ? `${prefix}/${path}/${item.name}` : `${prefix}/${item.name}`;
            files.push({
                name: item.name,
                path: filePath,
                size: item.metadata?.size || 0,
                created_at: item.created_at
            });
        }
    }

    return files;
}

async function scheduledCleanup() {
    const timestamp = new Date().toISOString();
    console.log(`[${timestamp}] Starting scheduled cleanup...`);

    const { data: devices, error } = await supabase.storage
        .from(BUCKET)
        .list('', { limit: 1000 });

    if (error) {
        console.error('Error listing devices:', error);
        return;
    }

    const allFiles = [];
    for (const device of devices) {
        if (device.id === null) {
            const files = await listAllFiles(device.name);
            allFiles.push(...files);
        }
    }

    const tinySImages = allFiles.filter(file => {
        return file.name.includes('_S_') && file.size < SIZE_THRESHOLD;
    });

    if (tinySImages.length === 0) {
        console.log(`[${timestamp}] No tiny _S images found. Storage is clean.`);
        return;
    }

    console.log(`[${timestamp}] Found ${tinySImages.length} tiny _S images to delete`);

    let deleted = 0;
    let errors = 0;

    for (const file of tinySImages) {
        try {
            const { error } = await supabase.storage
                .from(BUCKET)
                .remove([file.path]);

            if (error) throw error;
            deleted++;

            if (deleted % 10 === 0) {
                await new Promise(resolve => setTimeout(resolve, 100));
            }
        } catch (error) {
            errors++;
            console.error(`Error deleting ${file.path}:`, error.message);
        }
    }

    const totalSize = tinySImages.reduce((sum, f) => sum + f.size, 0);
    console.log(`[${timestamp}] Cleanup complete: ${deleted} deleted, ${errors} errors, ${(totalSize / 1024 / 1024).toFixed(2)} MB reclaimed`);
}

scheduledCleanup().catch(console.error);
