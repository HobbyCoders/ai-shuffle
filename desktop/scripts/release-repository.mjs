// Resolve publication identity before storing any asset provenance. No redirects.
export async function releaseRepository(token, env = process.env) {
 if (!token) throw Error('Release repository credentials are missing');
 const repository = env.RELEASE_GITHUB_REPO || 'HobbyCoders/ai-shuffle';
 if (!['HobbyCoders/ai-shuffle', 'HobbyCoders/ai-shuffle-private'].includes(repository)) throw Error('Unapproved release repository');
 const response = await fetch(`https://api.github.com/repos/${repository}`, {
  headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'User-Agent': 'AI-Shuffle-Publisher' },
  redirect: 'manual', signal: AbortSignal.timeout(20000),
 });
 if (response.status !== 200) throw Error('Release repository metadata unavailable');
 const metadata = await response.json();
 if (metadata.full_name !== repository || !Number.isSafeInteger(metadata.id) || metadata.id <= 0) throw Error('Release repository identity mismatch');
 if (!/^[1-9][0-9]*$/.test(env.RELEASE_GITHUB_REPO_ID || '') || Number(env.RELEASE_GITHUB_REPO_ID) !== metadata.id) throw Error('RELEASE_GITHUB_REPO_ID must pin the publication repository');
 return { repository, id: metadata.id };
}
