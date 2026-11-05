// Read-only GitHub requests share the Python evidence reader's fixed bounds.
// The response reader keeps repository JSON body timeouts inside the retry.
export async function releaseRead(url, options = {}, read = response => response) {
 for (let attempt = 0; attempt < 3; attempt++) {
  try {
   const response = await fetch(url, { ...options, method: 'GET', signal: AbortSignal.timeout(20000) });
   if (response.status >= 500 && response.status < 600 && attempt < 2) {
    await response.body?.cancel();
   } else {
    return await read(response);
   }
  } catch (error) {
   // fetch can time out or lose an established socket before our signal fires.
   const timeout = error?.name === 'TimeoutError' ||
    ['UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT', 'UND_ERR_BODY_TIMEOUT'].includes(error?.cause?.code ?? error?.code);
   const closed = error instanceof TypeError && ['fetch failed', 'terminated'].includes(error.message) &&
    ['UND_ERR_SOCKET', 'ECONNRESET', 'EPIPE', 'UND_ERR_CLOSED'].includes(error.cause?.code);
   // Refusal, DNS, TLS and unclassified failures can mean misconfiguration.
   if (attempt === 2 || !(timeout || closed)) throw error;
  }
  await new Promise(resolve => setTimeout(resolve, (attempt + 1) * 1000));
 }
}

// Resolve publication identity before storing any asset provenance. No redirects.
export async function releaseRepository(token, env = process.env) {
 if (!token) throw Error('Release repository credentials are missing');
 const repository = env.RELEASE_GITHUB_REPO || 'HobbyCoders/ai-shuffle';
 if (repository !== 'HobbyCoders/ai-shuffle') throw Error('Unapproved release repository');
 const metadata = await releaseRead(`https://api.github.com/repos/${repository}`, {
  headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'User-Agent': 'AI-Shuffle-Publisher' },
  redirect: 'manual',
 }, async response => {
  if (response.status !== 200) throw Error('Release repository metadata unavailable');
  return response.json();
 });
 if (metadata.full_name !== repository || !Number.isSafeInteger(metadata.id) || metadata.id <= 0) throw Error('Release repository identity mismatch');
 if (!/^[1-9][0-9]*$/.test(env.RELEASE_GITHUB_REPO_ID || '') || Number(env.RELEASE_GITHUB_REPO_ID) !== metadata.id) throw Error('RELEASE_GITHUB_REPO_ID must pin the publication repository');
 return { repository, id: metadata.id };
}
