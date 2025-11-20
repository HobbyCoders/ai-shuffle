# Kavita Integration Guide

This guide shows how to use the Claude Code SDK for manga scraping in Kavita.

## Overview

The SDK provides general-purpose AI endpoints that Kavita can use to:
- Extract structured data from HTML pages
- Parse manga metadata (titles, authors, genres, descriptions)
- Extract chapter lists from manga websites
- Transform unstructured web content into structured JSON

## Available Endpoints

### 1. Simple Chat (`POST /chat`)

Simple one-off prompts without context.

**Parameters:**
- `prompt` (required): The question/prompt to send
- `system_prompt` (optional): System instructions
- `model` (optional): `"sonnet"`, `"opus"`, or `"haiku"` (defaults to sonnet)

**Example:**
```http
POST http://your-unraid-ip:8000/chat
Content-Type: application/json

{
  "prompt": "What is the best way to parse HTML in C#?",
  "model": "haiku"
}
```

### 2. Structured Prompting (`POST /prompt/structured`)

The most powerful endpoint - perfect for data extraction with context.

**Parameters:**
- `user_prompt` (required): Your main question/instruction
- `system_prompt` (optional): System instructions to guide Claude's behavior
- `context` (optional): Additional data (HTML, JSON, code, etc.)
- `model` (optional): `"sonnet"`, `"opus"`, or `"haiku"`

**Use Case**: Extract manga metadata and chapters from any website

```http
POST http://your-unraid-ip:8000/prompt/structured
Content-Type: application/json

{
  "user_prompt": "Extract the manga series metadata and all available chapters. Return ONLY valid JSON with no markdown or extra text.",
  "system_prompt": "You are a manga metadata extractor. Return ONLY valid JSON in this exact format:\n{\n  \"metadata\": {\"title\": string, \"author\": string, \"artist\": string, \"genres\": [strings], \"description\": string, \"status\": string, \"year\": number},\n  \"chapters\": [{\"number\": string, \"title\": string, \"url\": string, \"releaseDate\": string}]\n}",
  "context": "<html>... the manga page HTML ...</html>",
  "model": "sonnet"
}
```

**Response:**
```json
{
  "response": "{\"metadata\": {\"title\": \"One Piece\", \"author\": \"Eiichiro Oda\", \"artist\": \"Eiichiro Oda\", \"genres\": [\"Action\", \"Adventure\"], \"description\": \"...\", \"status\": \"ongoing\", \"year\": 1997}, \"chapters\": [{\"number\": \"1\", \"title\": \"Romance Dawn\", \"url\": \"...\", \"releaseDate\": \"1997-07-22\"}]}",
  "status": "success",
  "metadata": {
    "model": "claude-sonnet-4-5-20250929",
    "duration_ms": 3500,
    "num_turns": 1,
    "total_cost_usd": 0.0087,
    "is_error": false
  }
}
```

### 3. File Analysis (`POST /analyze/file`)

Specialized endpoint for analyzing file content.

**Parameters:**
- `content` (required): The file content as a string
- `content_type` (required): Type of content - `"html"`, `"json"`, `"xml"`, `"code"`, or `"text"`
- `analysis_instructions` (required): What you want Claude to do with the content
- `model` (optional): `"sonnet"`, `"opus"`, or `"haiku"`

**Use Case**: Parse HTML specifically for manga data

```http
POST http://your-unraid-ip:8000/analyze/file
Content-Type: application/json

{
  "content": "<html>...</html>",
  "content_type": "html",
  "analysis_instructions": "Extract all chapter numbers, titles, and URLs. Return as a JSON array with no markdown formatting.",
  "model": "haiku"
}
```

### 4. Multi-Turn Conversation (`POST /conversation`)

Maintain conversation context across multiple messages.

**Parameters:**
- `messages` (required): Array of messages with `role` ("user") and `content`
- `system_prompt` (optional): System instructions
- `model` (optional): `"sonnet"`, `"opus"`, or `"haiku"`

**Example:**
```http
POST http://your-unraid-ip:8000/conversation
Content-Type: application/json

{
  "messages": [
    {"role": "user", "content": "I'm going to send you HTML from a manga site."},
    {"role": "user", "content": "Here it is: <html>...</html>"},
    {"role": "user", "content": "Now extract the title and author as JSON."}
  ],
  "model": "sonnet"
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
    private readonly ILogger<ClaudeSdkClient> _logger;

    public ClaudeSdkClient(string sdkBaseUrl, ILogger<ClaudeSdkClient> logger)
    {
        _sdkBaseUrl = sdkBaseUrl; // http://unraid-ip:8000
        _httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(120) };
        _logger = logger;
    }

    public async Task<MangaExtractionResult> ExtractMangaDataAsync(
        string html,
        string sourceUrl)
    {
        var request = new
        {
            user_prompt = "Extract manga series metadata and all chapters. Return ONLY valid JSON with no markdown formatting or code blocks.",
            system_prompt = @"You are a manga metadata extractor. Return ONLY valid JSON in this exact format:
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
            model = "haiku"  // Fast & cheap for scraping (sonnet/opus/haiku)
        };

        var response = await _httpClient.PostAsJsonAsync(
            $"{_sdkBaseUrl}/prompt/structured",
            request
        );

        response.EnsureSuccessStatusCode();

        var result = await response.Content
            .ReadFromJsonAsync<ClaudeApiResponse>();

        if (result?.Status == "success" && !string.IsNullOrEmpty(result.Response))
        {
            // Claude's response is the JSON string
            try
            {
                return JsonSerializer.Deserialize<MangaExtractionResult>(result.Response);
            }
            catch (JsonException ex)
            {
                _logger.LogError(ex, "Failed to parse Claude response as JSON. Raw response: {Response}",
                    result.Response);
                throw new Exception($"Invalid JSON response from Claude: {ex.Message}");
            }
        }

        throw new Exception($"Extraction failed: {result?.Status ?? "unknown error"}");
    }
}

// Response models
public class ClaudeApiResponse
{
    [JsonPropertyName("response")]
    public string Response { get; set; }

    [JsonPropertyName("status")]
    public string Status { get; set; }

    [JsonPropertyName("metadata")]
    public ClaudeMetadata Metadata { get; set; }
}

public class ClaudeMetadata
{
    [JsonPropertyName("model")]
    public string Model { get; set; }

    [JsonPropertyName("duration_ms")]
    public int DurationMs { get; set; }

    [JsonPropertyName("num_turns")]
    public int NumTurns { get; set; }

    [JsonPropertyName("total_cost_usd")]
    public decimal TotalCostUsd { get; set; }

    [JsonPropertyName("is_error")]
    public bool IsError { get; set; }
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
   - `"haiku"` - Fast, cheap, good for simple extraction (~$0.01 per request)
   - `"sonnet"` - Best balance for complex pages (default, ~$0.01-0.02 per request)
   - `"opus"` - Most accurate but expensive (~$0.05+ per request)

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
# 1. Test health and authentication
curl http://your-unraid-ip:8000/health

# 2. Test simple chat
curl -X POST http://your-unraid-ip:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Say hello",
    "model": "haiku"
  }'

# 3. Test with sample HTML
curl -X POST http://your-unraid-ip:8000/analyze/file \
  -H "Content-Type: application/json" \
  -d '{
    "content": "<html><h1>Test Manga</h1><p>by Author Name</p></html>",
    "content_type": "html",
    "analysis_instructions": "Extract the title and author as JSON",
    "model": "haiku"
  }'

# 4. Test structured prompting (recommended for Kavita)
curl -X POST http://your-unraid-ip:8000/prompt/structured \
  -H "Content-Type: application/json" \
  -d '{
    "user_prompt": "Extract the manga title and return it as JSON",
    "context": "<html><h1>One Piece</h1></html>",
    "system_prompt": "Return only valid JSON",
    "model": "haiku"
  }'
```

## Configuration in Kavita

Add to `appsettings.json`:

```json
{
  "Scraper": {
    "ClaudeSdkUrl": "http://your-unraid-ip:8000",
    "DefaultModel": "haiku",
    "RequestTimeout": 120,
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

### Claude Returns Markdown-Formatted JSON

**Cause**: Claude wrapped JSON in markdown code blocks (```json ... ```)

**Solution**: Be explicit in prompts to avoid markdown
```json
{
  "user_prompt": "Extract the data. Return ONLY valid JSON with no markdown formatting or code blocks.",
  "system_prompt": "Return ONLY the JSON object. Start with { and end with }. No markdown, no explanation."
}
```

Then in C#, strip markdown if needed:
```csharp
var response = result.Response;
// Remove markdown code blocks if present
if (response.StartsWith("```"))
{
    response = Regex.Replace(response, @"^```(json)?\s*|\s*```$", "").Trim();
}
var data = JsonSerializer.Deserialize<MangaExtractionResult>(response);
```

### Extraction Misses Some Chapters

**Cause**: HTML context truncated (50k char limit)

**Solution**: Pre-filter HTML to just the chapter list section before sending

### High Latency

**Cause**: Large HTML + Claude processing time

**Solutions**:
- Use `"haiku"` model (fastest, ~1-3s response time)
- Filter HTML to relevant sections only before sending
- Cache results aggressively in Kavita database
- Process scraping in background jobs, not on-demand

**Benchmark Response Times:**
- Haiku: 1-3 seconds for typical manga pages
- Sonnet: 3-5 seconds for complex pages
- Opus: 5-10 seconds (use only when accuracy is critical)

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
