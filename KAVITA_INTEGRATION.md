# Kavita Integration Guide

This guide shows how to use the Claude Code SDK for manga scraping in Kavita.

## Overview

The SDK provides general-purpose AI endpoints that Kavita can use to:
- Extract structured data from HTML pages
- Parse manga metadata (titles, authors, genres, descriptions)
- Extract chapter lists from manga websites
- Transform unstructured web content into structured JSON

## Key Endpoints for Kavita

### 1. Structured Prompting (`POST /prompt/structured`)

The most flexible endpoint for custom extraction tasks.

**Use Case**: Extract manga metadata and chapters from any website

```http
POST http://your-unraid-ip:8000/prompt/structured
Content-Type: application/json

{
  "user_prompt": "Extract the manga series metadata and all available chapters from this HTML",
  "system_prompt": "You are a manga metadata extractor. Return ONLY valid JSON in this exact format:\n{\n  \"metadata\": {\"title\": string, \"author\": string, \"artist\": string, \"genres\": [strings], \"description\": string, \"status\": string, \"year\": number},\n  \"chapters\": [{\"number\": string, \"title\": string, \"url\": string, \"releaseDate\": string}]\n}",
  "context": "<html>... the manga page HTML ...</html>",
  "json_mode": true,
  "model": "claude-sonnet-4"
}
```

**Response**:
```json
{
  "success": true,
  "response": "{\"metadata\": {...}, \"chapters\": [...]}",
  "parsed_json": {
    "metadata": {
      "title": "One Piece",
      "author": "Eiichiro Oda",
      "genres": ["Action", "Adventure"],
      ...
    },
    "chapters": [
      {"number": "1", "title": "Romance Dawn", "url": "..."},
      ...
    ]
  },
  "metadata": {
    "json_parsed": true
  }
}
```

### 2. File Analysis (`POST /analyze/file`)

Specialized endpoint for analyzing HTML, JSON, XML, etc.

**Use Case**: Parse HTML specifically for manga data

```http
POST http://your-unraid-ip:8000/analyze/file
Content-Type: application/json

{
  "content": "<html>...</html>",
  "content_type": "html",
  "analysis_instructions": "Extract all chapter numbers, titles, and URLs. Return as JSON array.",
  "output_format": "json",
  "model": "claude-haiku-4"
}
```

## Integration Architecture

### From Kavita's Perspective

```
┌─────────────────────────────────────────┐
│         Kavita (.NET Backend)           │
│                                         │
│  1. Fetch HTML from manga site          │
│  2. Send to Claude SDK via HTTP         │
│  3. Receive structured JSON             │
│  4. Save to database                    │
└────────────────┬────────────────────────┘
                 │ HTTP POST
                 ▼
┌─────────────────────────────────────────┐
│    Claude SDK (Unraid Container)        │
│                                         │
│  - Receives HTML + instructions         │
│  - Builds prompt with context           │
│  - Calls Claude via OAuth               │
│  - Extracts & parses JSON               │
│  - Returns structured data              │
└─────────────────────────────────────────┘
```

## Example: Kavita Scraper Service Implementation

### C# Service (in Kavita)

```csharp
public class ClaudeSdkClient
{
    private readonly HttpClient _httpClient;
    private readonly string _sdkBaseUrl;

    public ClaudeSdkClient(string sdkBaseUrl)
    {
        _sdkBaseUrl = sdkBaseUrl; // http://unraid-ip:8000
        _httpClient = new HttpClient();
    }

    public async Task<MangaExtractionResult> ExtractMangaDataAsync(
        string html,
        string sourceUrl)
    {
        var request = new
        {
            user_prompt = "Extract manga series metadata and all chapters",
            system_prompt = @"You are a manga metadata extractor. Return ONLY valid JSON:
{
  ""metadata"": {
    ""title"": string,
    ""author"": string,
    ""artist"": string,
    ""genres"": [strings],
    ""description"": string,
    ""status"": ""ongoing"" | ""completed"",
    ""year"": number
  },
  ""chapters"": [
    {
      ""number"": string,
      ""title"": string,
      ""url"": string,
      ""releaseDate"": string
    }
  ]
}",
            context = html,
            json_mode = true,
            model = "claude-haiku-4"  // Fast & cheap for scraping
        };

        var response = await _httpClient.PostAsJsonAsync(
            $"{_sdkBaseUrl}/prompt/structured",
            request
        );

        var result = await response.Content
            .ReadFromJsonAsync<ClaudeResponse>();

        if (result.Success && result.ParsedJson != null)
        {
            return JsonSerializer.Deserialize<MangaExtractionResult>(
                result.ParsedJson.ToString()
            );
        }

        throw new Exception($"Extraction failed: {result.Error}");
    }
}
```

### Usage in Kavita's Scraper Service

```csharp
public class ScraperService
{
    private readonly ClaudeSdkClient _claudeClient;
    private readonly IHttpClientFactory _httpFactory;

    public async Task ScrapeSeriesAsync(int seriesId, string externalUrl)
    {
        // 1. Fetch HTML from source
        var html = await _httpFactory.CreateClient()
            .GetStringAsync(externalUrl);

        // 2. Extract data using Claude
        var extractedData = await _claudeClient
            .ExtractMangaDataAsync(html, externalUrl);

        // 3. Update series in database
        var series = await _unitOfWork.SeriesRepository
            .GetSeriesByIdAsync(seriesId);

        series.Name = extractedData.Metadata.Title;
        series.Summary = extractedData.Metadata.Description;
        // ... update other fields

        // 4. Create chapter records
        foreach (var chapter in extractedData.Chapters)
        {
            var chapterRecord = new ScraperResult
            {
                ChapterNumber = chapter.Number,
                ChapterTitle = chapter.Title,
                ScrapedUrl = chapter.Url,
                Status = ResultStatus.Pending
            };
            _unitOfWork.ScraperRepository.AddResult(chapterRecord);
        }

        await _unitOfWork.CommitAsync();
    }
}
```

## Prompt Engineering Tips

### For Best Results

1. **Be Specific About Format**
   ```json
   {
     "system_prompt": "Return ONLY valid JSON. No markdown, no explanation."
   }
   ```

2. **Provide Schema Examples**
   ```json
   {
     "system_prompt": "Return JSON matching this exact structure:\n{\"chapters\": [{\"number\": \"1\", \"title\": \"...\"}]}"
   }
   ```

3. **Use Context Wisely**
   - Send full HTML for initial extraction
   - For chapter lists only, can send filtered HTML (just the chapter list section)

4. **Model Selection**
   - `claude-haiku-4` - Fast, cheap, good for simple extraction
   - `claude-sonnet-4` - Best balance for complex pages
   - `claude-opus-4` - Most accurate but expensive

## Error Handling

```csharp
try
{
    var result = await _claudeClient.ExtractMangaDataAsync(html, url);

    if (result == null)
    {
        _logger.LogWarning("Claude returned null data for {Url}", url);
        // Fallback to CSS selectors or manual parsing
        return await FallbackExtraction(html);
    }
}
catch (HttpRequestException ex)
{
    _logger.LogError(ex, "Claude SDK unreachable");
    // Mark for retry
}
catch (JsonException ex)
{
    _logger.LogError(ex, "Invalid JSON from Claude");
    // Log raw response for debugging
}
```

## Rate Limiting

The SDK itself doesn't rate limit, but Claude Code may have OAuth limits.

**Recommendations**:
- Implement rate limiting in Kavita (e.g., 30 requests/minute)
- Queue scraping jobs with delays
- Cache results to avoid re-scraping

```csharp
// In Kavita's TaskScheduler
RecurringJob.AddOrUpdate(
    "scrape-queue",
    () => ProcessScrapingQueueWithRateLimitAsync(),
    "*/5 * * * *"  // Every 5 minutes
);

private async Task ProcessScrapingQueueWithRateLimitAsync()
{
    var pending = await GetPendingScrapingTasksAsync();

    foreach (var task in pending.Take(10))  // Max 10 per run
    {
        await ScrapeSeriesAsync(task.SeriesId, task.Url);
        await Task.Delay(2000);  // 2 second delay between requests
    }
}
```

## Testing

Test the SDK independently before integrating:

```bash
# 1. Test authentication
curl http://your-unraid-ip:8000/auth/status

# 2. Test with sample HTML
curl -X POST http://your-unraid-ip:8000/analyze/file \
  -H "Content-Type: application/json" \
  -d '{
    "content": "<html><h1>Test Manga</h1></html>",
    "content_type": "html",
    "analysis_instructions": "Extract the title",
    "output_format": "json"
  }'

# 3. Test structured prompting
curl -X POST http://your-unraid-ip:8000/prompt/structured \
  -H "Content-Type: application/json" \
  -d '{
    "user_prompt": "What is this?",
    "context": "<html>manga page</html>",
    "json_mode": true
  }'
```

## Configuration in Kavita

Add to `appsettings.json`:

```json
{
  "Scraper": {
    "ClaudeSdkUrl": "http://your-unraid-ip:8000",
    "DefaultModel": "claude-haiku-4",
    "RequestTimeout": 300,
    "RateLimitPerMinute": 30,
    "EnableAutomaticScraping": true
  }
}
```

## Advantages Over Traditional API Approach

✅ **No API Key Management** - Uses OAuth, no keys to rotate
✅ **Cost Effective** - Uses Claude Code's pricing (cheaper than API)
✅ **Flexible** - Works with any website structure
✅ **Self-Hosted** - Runs on your Unraid, no external dependencies
✅ **Adaptive** - Claude figures out page structure automatically
✅ **No Selectors** - No CSS selectors to maintain per-site

## Troubleshooting

### SDK Returns Empty parsed_json

**Cause**: Claude returned text instead of JSON

**Solution**: Make system prompt more explicit
```json
{
  "system_prompt": "CRITICAL: Return ONLY the JSON object. Start with { and end with }. No other text."
}
```

### Extraction Misses Some Chapters

**Cause**: HTML context truncated (50k char limit)

**Solution**: Pre-filter HTML to just the chapter list section before sending

### High Latency

**Cause**: Large HTML + Claude processing time

**Solutions**:
- Use `claude-haiku-4` model (fastest)
- Filter HTML to relevant sections only
- Cache results aggressively

## Next Steps

1. Implement `ClaudeSdkClient` in Kavita
2. Add configuration settings
3. Test with a few manga sites
4. Tune prompts for best extraction
5. Implement error handling and retries
6. Add monitoring and logging
7. Deploy to production

## Support

- SDK Issues: Check Claude SDK repo
- Integration Issues: Kavita GitHub
- Prompt Engineering: Anthropic docs
