// Test deleting a single file
import { createClient } from '@supabase/supabase-js';
import { getSupabaseRuntimeConfig } from './supabase-runtime-config.mjs';

const { url: SUPABASE_URL, key: SUPABASE_KEY, bucket: BUCKET } = getSupabaseRuntimeConfig();

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

async function testDelete() {
    const testPath = 'QN/PICT3821_S_202603090102WL48X.jpg';

    console.log(`Attempting to delete: ${testPath}`);

    const { data, error } = await supabase.storage
        .from(BUCKET)
        .remove([testPath]);

    console.log('\nResult:');
    console.log('data:', JSON.stringify(data, null, 2));
    console.log('error:', error);

    if (error) {
        console.log('\n❌ Error details:');
        console.log('Message:', error.message);
        console.log('Status:', error.statusCode);
        console.log('Full error:', JSON.stringify(error, null, 2));
    } else {
        console.log('\n✓ API call succeeded');

        // Verify if file still exists
        console.log('\nVerifying deletion...');
        const { data: files, error: listError } = await supabase.storage
            .from(BUCKET)
            .list('QN', { search: 'PICT3821' });

        if (listError) {
            console.log('Error checking:', listError);
        } else {
            console.log(`Files found: ${files.length}`);
            files.forEach(f => console.log(`  - ${f.name} (${f.metadata?.size} bytes)`));
        }
    }
}

testDelete().catch(console.error);
