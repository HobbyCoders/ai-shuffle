# GitHub Container Registry Setup

This document explains how to set up and use the automated Docker image publishing to GitHub Container Registry (ghcr.io).

## For Repository Owners

### Initial Setup

The GitHub Actions workflow is already configured in `.github/workflows/docker-publish.yml`. To enable it:

1. **Enable GitHub Actions** (if not already enabled)
   - Go to your repository Settings → Actions → General
   - Ensure "Allow all actions and reusable workflows" is selected

2. **Configure Package Visibility**
   - After the first successful build, go to your GitHub profile
   - Click "Packages" tab
   - Find `proxy-python-sdk`
   - Click "Package settings"
   - Under "Danger Zone", change visibility to Public (recommended) or keep Private

3. **Optional: Add Repository Secret for Custom Registry**
   - The workflow uses `GITHUB_TOKEN` automatically (no setup needed)
   - For custom registries, add secrets in Settings → Secrets and variables → Actions

### How It Works

The workflow automatically builds and pushes Docker images when:

- **Push to main/master branch** → Tagged as `latest`
- **Create version tag** (e.g., `v1.0.0`) → Tagged with version numbers
- **Manual trigger** → Via Actions tab "Run workflow" button

### Creating a Release

To publish a versioned release:

```bash
# Tag your release
git tag v1.0.0
git push origin v1.0.0
```

This will automatically build and push images tagged:
- `ghcr.io/your-username/proxy-python-sdk:1.0.0`
- `ghcr.io/your-username/proxy-python-sdk:1.0`
- `ghcr.io/your-username/proxy-python-sdk:1`
- `ghcr.io/your-username/proxy-python-sdk:latest` (if on main branch)

## For Users

### Pulling Pre-built Images

#### Public Repository

If the package is public, anyone can pull:

```bash
docker pull ghcr.io/<username>/proxy-python-sdk:latest
```

#### Private Repository

For private packages, authenticate first:

```bash
# Create a Personal Access Token (PAT):
# GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
# Scope required: read:packages

# Login to ghcr.io
echo $GITHUB_PAT | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin

# Pull the image
docker pull ghcr.io/<username>/proxy-python-sdk:latest
```

### Using in docker-compose.yml

The docker-compose.yml is pre-configured to use ghcr.io:

```yaml
services:
  claude-sdk:
    image: ghcr.io/<your-username>/proxy-python-sdk:latest
```

Just replace `<your-username>` with the actual GitHub username.

### Available Tags

- `latest` - Latest build from main branch (always up-to-date)
- `v1.0.0` - Specific version (stable, doesn't change)
- `v1.0` - Latest patch in v1.0.x series
- `v1` - Latest minor in v1.x.x series
- `main` - Latest main branch build
- `main-abc1234` - Specific commit SHA

### Supported Architectures

Images are built for multiple architectures:
- `linux/amd64` - Intel/AMD 64-bit (most common)
- `linux/arm64` - ARM 64-bit (Raspberry Pi 4/5, Apple Silicon)

Docker automatically pulls the correct architecture for your system.

## Troubleshooting

### Build Failed

Check the Actions tab in GitHub:
1. Go to repository → Actions
2. Click the failed workflow run
3. Expand the failed step to see error logs

Common issues:
- Dockerfile syntax errors
- Missing dependencies
- Build timeouts (increase in workflow file)

### Cannot Pull Image

**Error: "denied: permission_denied"**

Solutions:
- Ensure package visibility is set to Public (for public access)
- Authenticate with a PAT if private (see above)
- Check PAT has `read:packages` scope

**Error: "manifest unknown"**

Solutions:
- Verify the tag exists (check Packages on GitHub)
- Ensure the build completed successfully (check Actions)
- Try pulling `latest` tag explicitly

### Image Not Updating

If pulling `latest` doesn't get the newest version:

```bash
# Force remove cached image
docker rmi ghcr.io/<username>/proxy-python-sdk:latest

# Pull fresh
docker pull ghcr.io/<username>/proxy-python-sdk:latest

# Or use --pull=always with docker-compose
docker-compose pull
docker-compose up -d
```

## Workflow Configuration

The workflow is configured in `.github/workflows/docker-publish.yml`:

```yaml
on:
  push:
    branches: [main, master]
    tags: ['v*.*.*']
  workflow_dispatch:  # Manual trigger
```

### Customization

Edit `.github/workflows/docker-publish.yml` to:

- Add more platforms: `platforms: linux/amd64,linux/arm64,linux/arm/v7`
- Change tag patterns: Modify the `tags:` section
- Add build arguments: Use `build-args:` in build step
- Set up multi-stage builds: No changes needed, just works

## Best Practices

1. **Use version tags for production**
   ```yaml
   image: ghcr.io/user/proxy-python-sdk:v1.0.0  # Stable
   ```

2. **Use latest for development**
   ```yaml
   image: ghcr.io/user/proxy-python-sdk:latest  # Auto-updates
   ```

3. **Pin major versions for compatibility**
   ```yaml
   image: ghcr.io/user/proxy-python-sdk:v1  # Gets v1.x.x updates
   ```

4. **Test before tagging releases**
   ```bash
   # Test on main first
   docker pull ghcr.io/user/proxy-python-sdk:main

   # If good, create release tag
   git tag v1.0.1 && git push origin v1.0.1
   ```

## Manual Publishing (Alternative)

If you prefer to build and push manually:

```bash
# Build for multiple platforms
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t ghcr.io/<username>/proxy-python-sdk:latest \
  --push \
  .

# Or build for single platform
docker build -t ghcr.io/<username>/proxy-python-sdk:latest .
docker push ghcr.io/<username>/proxy-python-sdk:latest
```

## Security Notes

- GitHub Actions use `GITHUB_TOKEN` - automatically secured, expires quickly
- Never commit PATs or tokens to repository
- Use repository secrets for sensitive values
- Regular PATs for pulling should have minimal scopes (`read:packages` only)
- Enable Dependabot for automatic dependency updates

## Resources

- [GitHub Packages Documentation](https://docs.github.com/en/packages)
- [GitHub Actions Documentation](https://docs.github.com/en/actions)
- [Docker Buildx Documentation](https://docs.docker.com/build/buildx/)
