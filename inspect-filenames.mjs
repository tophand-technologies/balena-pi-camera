// Inspect actual filenames in Supabase storage
import { createClient } from '@supabase/supabase-js';
import { getSupabaseRuntimeConfig } from './supabase-runtime-config.mjs';

const { url: SUPABASE_URL, key: SUPABASE_KEY, bucket: BUCKET } = getSupabaseRuntimeConfig();

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

async function inspectFiles() {
    // Get a sample from QN folder
    const { data: files, error } = await supabase.storage
        .from(BUCKET)
        .list('QN', {
            limit: 20,
            sortBy: { column: 'created_at', order: 'desc' }
        });

    if (error) {
        console.error('Error:', error);
        return;
    }

    console.log('Sample files from QN folder:\n');
    files.forEach(file => {
        if (file.id !== null) { // Only files, not folders
            const sizeKB = (file.metadata?.size || 0) / 1024;
            console.log(`${file.name} - ${sizeKB.toFixed(2)} KB`);
        }
    });

    // Show files under 10KB
    console.log('\n\nFiles under 10 KB:');
    const smallFiles = files.filter(f => f.id !== null && (f.metadata?.size || 0) < 10240);
    smallFiles.forEach(file => {
        const sizeKB = (file.metadata?.size || 0) / 1024;
        console.log(`${file.name} - ${sizeKB.toFixed(2)} KB`);
    });

    console.log(`\nTotal small files (< 10KB): ${smallFiles.length} out of ${files.filter(f => f.id !== null).length} files`);
}

inspectFiles().catch(console.error);
