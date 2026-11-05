// Resolve publication identity before storing any asset provenance. No redirects.
export async function releaseRepository(token, env = process.env) {
 if (!token) throw Error('Release repository credentials are missing');
 const repository = env.RELEASE_GITHUB_REPO || 'HobbyCoders/ai-shuffle';
 if (repository !== 'HobbyCoders/ai-shuffle') throw Error('Unapproved release repository');
 let metadata;
 for (let attempt = 0; attempt < 3; attempt++) {
  try {
   const response = await fetch(`https://api.github.com/repos/${repository}`, {
    method: 'GET',
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'User-Agent': 'AI-Shuffle-Publisher' },
    redirect: 'manual', signal: AbortSignal.timeout(20000),
   });
   if (response.status >= 500 && response.status < 600 && attempt < 2) {
    await response.body?.cancel();
   } else {
    if (response.status !== 200) throw Error('Release repository metadata unavailable');
    metadata = await response.json();
    break;
   }
  } catch (error) {
   // fetch can hit its own transport timeout before our 20-second signal.
   const timeout = error?.name === 'TimeoutError' ||
    ['UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT', 'UND_ERR_BODY_TIMEOUT'].includes(error?.cause?.code ?? error?.code);
   if (attempt === 2 || !timeout) throw error;
  }
  // Same fixed bounds as the Python evidence reader; never retry writes.
  await new Promise(resolve => setTimeout(resolve, (attempt + 1) * 1000));
 }
 if (metadata.full_name !== repository || !Number.isSafeInteger(metadata.id) || metadata.id <= 0) throw Error('Release repository identity mismatch');
 if (!/^[1-9][0-9]*$/.test(env.RELEASE_GITHUB_REPO_ID || '') || Number(env.RELEASE_GITHUB_REPO_ID) !== metadata.id) throw Error('RELEASE_GITHUB_REPO_ID must pin the publication repository');
 return { repository, id: metadata.id };
}
