# CLAUDE CODE IMPLEMENTATION PROMPT: Kavita Manga Scraping with Claude Agent SDK

## Project Overview

You are implementing an AI-powered manga scraping system for Kavita (a .NET 8 manga/comic server). Instead of using the Anthropic API directly, you will integrate with a self-hosted Claude Python Agent SDK running on the user's Unraid server.

**Architecture**:
```
Kavita (.NET 8 Backend)
    ↓ HTTP POST
Claude Agent SDK (Unraid Docker Container - http://unraid-ip:8000)
    ↓ subprocess
Claude Code CLI (OAuth authenticated)
    ↓ API
Claude AI
```

## Claude Agent SDK Endpoints Available

The SDK provides these endpoints for your use:

### 1. `/prompt/structured` (Primary endpoint for scraping)
Most flexible - use for extracting structured data from HTML.

**Request**:
```json
{
  "user_prompt": "Extract manga metadata and chapters from this HTML",
  "system_prompt": "You are a manga metadata extractor. Return ONLY valid JSON...",
  "context": "<html>...</html>",
  "json_mode": true,
  "model": "claude-haiku-4"
}
```

**Response**:
```json
{
  "success": true,
  "response": "{...}",
  "parsed_json": { /* Already parsed JSON object */ },
  "metadata": { "json_parsed": true }
}
```

### 2. `/analyze/file` (For HTML analysis)
Specialized content analysis with built-in HTML expertise.

**Request**:
```json
{
  "content": "<html>...</html>",
  "content_type": "html",
  "analysis_instructions": "Extract all chapter links",
  "output_format": "json"
}
```

### 3. `/conversation` (For multi-turn interactions)
Use if you need contextual follow-up questions.

## Implementation Requirements

### Tech Stack
- **.NET 8** with ASP.NET Core
- **Entity Framework Core** with SQLite
- **Hangfire** for background jobs
- **Flurl or HttpClient** for HTTP requests to SDK
- **Serilog** for logging
- **SignalR** for real-time updates (optional)

### Architecture Pattern
- **Repository Pattern** with Unit of Work
- **Service Layer** for business logic
- **DTOs** for API contracts
- **Background Jobs** for scraping queue

## Step-by-Step Implementation

### PHASE 1: Database Schema

Create entities in `API/Entities/Scraping/`:

#### ScraperConfiguration.cs
```csharp
public class ScraperConfiguration
{
    public int Id { get; set; }
    public string Name { get; set; } = string.Empty; // "MangaDex", "MangaPlus"
    public ScraperSourceType SourceType { get; set; }
    public string BaseUrl { get; set; } = string.Empty; // "https://mangadex.org"
    public bool IsEnabled { get; set; } = true;
    public int Priority { get; set; } = 5; // 1-10
    public int RateLimitPerMinute { get; set; } = 30;

    // Claude SDK Configuration
    public string ClaudePromptTemplate { get; set; } = string.Empty; // JSON schema for extraction
    public string? HeadersConfig { get; set; } // JSON serialized headers

    public DateTime Created { get; set; }
    public DateTime CreatedUtc { get; set; }
    public DateTime LastModified { get; set; }
    public DateTime LastModifiedUtc { get; set; }

    // Navigation
    public ICollection<ScraperMapping> ScraperMappings { get; set; } = null!;
}

public enum ScraperSourceType
{
    MangaDex = 0,
    MangaPlus = 1,
    Custom = 99
}
```

#### ScraperMapping.cs
```csharp
public class ScraperMapping
{
    public int Id { get; set; }
    public int SeriesId { get; set; } // FK to Series
    public int ScraperConfigurationId { get; set; } // FK to ScraperConfiguration

    public string ExternalId { get; set; } = string.Empty; // e.g., MangaDex UUID
    public string? ExternalUrl { get; set; }

    public DateTime? LastScrapedDate { get; set; }
    public DateTime? NextScheduledScrape { get; set; }
    public bool IsActive { get; set; } = true;

    public int FailureCount { get; set; } = 0;
    public string? LastError { get; set; }

    public DateTime Created { get; set; }
    public DateTime CreatedUtc { get; set; }

    // Navigation
    public Series Series { get; set; } = null!;
    public ScraperConfiguration ScraperConfiguration { get; set; } = null!;
    public ICollection<ScraperResult> ScraperResults { get; set; } = null!;
}
```

#### ScraperResult.cs
```csharp
public class ScraperResult
{
    public int Id { get; set; }
    public int ScraperMappingId { get; set; } // FK

    public string ChapterNumber { get; set; } = string.Empty; // "23.5"
    public string? ChapterTitle { get; set; }
    public string ScrapedUrl { get; set; } = string.Empty;

    public ResultStatus Status { get; set; } = ResultStatus.Pending;
    public DateTime ScrapedDate { get; set; }
    public DateTime? ReleaseDate { get; set; }

    public int? PageCount { get; set; }
    public string? DownloadPath { get; set; }
    public string? ErrorMessage { get; set; }

    // Navigation
    public ScraperMapping ScraperMapping { get; set; } = null!;
}

public enum ResultStatus
{
    Pending = 0,
    Downloading = 1,
    Completed = 2,
    Failed = 3,
    Duplicate = 4
}
```

#### ScraperLog.cs
```csharp
public class ScraperLog
{
    public int Id { get; set; }
    public int ScraperMappingId { get; set; }
    public LogLevel LogLevel { get; set; }
    public string Message { get; set; } = string.Empty;
    public string? Details { get; set; }
    public DateTime CreatedUtc { get; set; }
}
```

**Database Configuration in `DataContext.cs`**:
```csharp
public DbSet<ScraperConfiguration> ScraperConfigurations => Set<ScraperConfiguration>();
public DbSet<ScraperMapping> ScraperMappings => Set<ScraperMapping>();
public DbSet<ScraperResult> ScraperResults => Set<ScraperResult>();
public DbSet<ScraperLog> ScraperLogs => Set<ScraperLog>();

protected override void OnModelCreating(ModelBuilder modelBuilder)
{
    // ... existing configurations ...

    modelBuilder.Entity<ScraperConfiguration>(entity =>
    {
        entity.HasKey(e => e.Id);
        entity.HasIndex(e => e.IsEnabled);
        entity.HasIndex(e => e.BaseUrl);
    });

    modelBuilder.Entity<ScraperMapping>(entity =>
    {
        entity.HasKey(e => e.Id);
        entity.HasIndex(e => new { e.SeriesId, e.ScraperConfigurationId }).IsUnique();
        entity.HasIndex(e => e.IsActive);
        entity.HasIndex(e => e.NextScheduledScrape);

        entity.HasOne(e => e.Series)
            .WithMany()
            .HasForeignKey(e => e.SeriesId)
            .OnDelete(DeleteBehavior.Cascade);

        entity.HasOne(e => e.ScraperConfiguration)
            .WithMany(c => c.ScraperMappings)
            .HasForeignKey(e => e.ScraperConfigurationId)
            .OnDelete(DeleteBehavior.Cascade);
    });

    modelBuilder.Entity<ScraperResult>(entity =>
    {
        entity.HasKey(e => e.Id);
        entity.HasIndex(e => new { e.ScraperMappingId, e.ChapterNumber });
        entity.HasIndex(e => e.Status);

        entity.HasOne(e => e.ScraperMapping)
            .WithMany(m => m.ScraperResults)
            .HasForeignKey(e => e.ScraperMappingId)
            .OnDelete(DeleteBehavior.Cascade);
    });
}
```

**Create Migration**:
```bash
dotnet ef migrations add AddScraperEntities --context DataContext
dotnet ef database update
```

---

### PHASE 2: Repository Layer

#### IScraperRepository.cs (`API/Repositories/`)
```csharp
public interface IScraperRepository
{
    // Configuration
    Task<IEnumerable<ScraperConfiguration>> GetAllScraperConfigurationsAsync();
    Task<ScraperConfiguration?> GetScraperConfigurationAsync(int id);
    void AddScraperConfiguration(ScraperConfiguration config);
    void UpdateScraperConfiguration(ScraperConfiguration config);
    void DeleteScraperConfiguration(ScraperConfiguration config);

    // Mapping
    Task<IEnumerable<ScraperMapping>> GetMappingsBySeriesIdAsync(int seriesId);
    Task<ScraperMapping?> GetScraperMappingAsync(int id);
    Task<ScraperMapping?> GetMappingBySeriesAndConfigAsync(int seriesId, int configId);
    Task<IEnumerable<ScraperMapping>> GetDueForScrapingAsync();
    void AddScraperMapping(ScraperMapping mapping);
    void UpdateScraperMapping(ScraperMapping mapping);
    void DeleteScraperMapping(ScraperMapping mapping);

    // Results
    Task<IEnumerable<ScraperResult>> GetPendingResultsAsync();
    Task<ScraperResult?> GetScraperResultAsync(int id);
    void AddScraperResult(ScraperResult result);
    void UpdateScraperResult(ScraperResult result);

    // Logs
    Task<IEnumerable<ScraperLog>> GetRecentLogsAsync(int count = 100);
    void AddLog(ScraperLog log);
    Task CleanupOldLogsAsync(DateTime cutoffDate);
}
```

#### ScraperRepository.cs Implementation
```csharp
public class ScraperRepository : IScraperRepository
{
    private readonly DataContext _context;

    public ScraperRepository(DataContext context)
    {
        _context = context;
    }

    // Implement all interface methods using EF Core
    // Use .Include() for navigation properties
    // Example:

    public async Task<ScraperMapping?> GetScraperMappingAsync(int id)
    {
        return await _context.ScraperMappings
            .Include(m => m.Series)
            .Include(m => m.ScraperConfiguration)
            .Include(m => m.ScraperResults)
            .FirstOrDefaultAsync(m => m.Id == id);
    }

    public async Task<IEnumerable<ScraperMapping>> GetDueForScrapingAsync()
    {
        var now = DateTime.UtcNow;
        return await _context.ScraperMappings
            .Include(m => m.Series)
            .Include(m => m.ScraperConfiguration)
            .Where(m => m.IsActive &&
                       m.ScraperConfiguration.IsEnabled &&
                       (m.NextScheduledScrape == null || m.NextScheduledScrape <= now))
            .OrderBy(m => m.ScraperConfiguration.Priority)
            .ToListAsync();
    }

    // ... implement other methods
}
```

**Add to IUnitOfWork and UnitOfWork**:
```csharp
// IUnitOfWork.cs
public interface IUnitOfWork
{
    // ... existing repositories
    IScraperRepository ScraperRepository { get; }
}

// UnitOfWork.cs
private readonly Lazy<IScraperRepository> _scraperRepository;

public IScraperRepository ScraperRepository => _scraperRepository.Value;

// In constructor:
_scraperRepository = new Lazy<IScraperRepository>(() => new ScraperRepository(_context));
```

---

### PHASE 3: Claude SDK Client Service

#### ClaudeSdkClient.cs (`API/Services/Scraping/`)
```csharp
public interface IClaudeSdkClient
{
    Task<ClaudeSdkResponse> ExtractStructuredDataAsync(
        string html,
        string userPrompt,
        string systemPrompt,
        string model = "claude-haiku-4");

    Task<ClaudeSdkResponse> AnalyzeHtmlAsync(
        string html,
        string instructions,
        string outputFormat = "json");
}

public class ClaudeSdkClient : IClaudeSdkClient
{
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly ILogger<ClaudeSdkClient> _logger;
    private readonly string _sdkBaseUrl;

    public ClaudeSdkClient(
        IHttpClientFactory httpClientFactory,
        ILogger<ClaudeSdkClient> logger,
        IConfiguration config)
    {
        _httpClientFactory = httpClientFactory;
        _logger = logger;
        _sdkBaseUrl = config["Scraper:ClaudeSdkUrl"] ?? "http://localhost:8000";
    }

    public async Task<ClaudeSdkResponse> ExtractStructuredDataAsync(
        string html,
        string userPrompt,
        string systemPrompt,
        string model = "claude-haiku-4")
    {
        var client = _httpClientFactory.CreateClient("ClaudeSdk");

        var request = new
        {
            user_prompt = userPrompt,
            system_prompt = systemPrompt,
            context = html,
            json_mode = true,
            model = model
        };

        try
        {
            var response = await client.PostAsJsonAsync(
                $"{_sdkBaseUrl}/prompt/structured",
                request);

            response.EnsureSuccessStatusCode();

            var result = await response.Content.ReadFromJsonAsync<ClaudeSdkResponse>();

            if (result == null || !result.Success)
            {
                _logger.LogError("[ClaudeSdk] Failed to extract data: {Error}",
                    result?.Error ?? "Unknown error");
                return new ClaudeSdkResponse { Success = false, Error = "Extraction failed" };
            }

            return result;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "[ClaudeSdk] Error calling SDK");
            return new ClaudeSdkResponse
            {
                Success = false,
                Error = $"SDK error: {ex.Message}"
            };
        }
    }

    public async Task<ClaudeSdkResponse> AnalyzeHtmlAsync(
        string html,
        string instructions,
        string outputFormat = "json")
    {
        var client = _httpClientFactory.CreateClient("ClaudeSdk");

        var request = new
        {
            content = html,
            content_type = "html",
            analysis_instructions = instructions,
            output_format = outputFormat,
            model = "claude-haiku-4"
        };

        try
        {
            var response = await client.PostAsJsonAsync(
                $"{_sdkBaseUrl}/analyze/file",
                request);

            response.EnsureSuccessStatusCode();

            return await response.Content.ReadFromJsonAsync<ClaudeSdkResponse>()
                ?? new ClaudeSdkResponse { Success = false, Error = "Empty response" };
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "[ClaudeSdk] Error analyzing HTML");
            return new ClaudeSdkResponse { Success = false, Error = ex.Message };
        }
    }
}

// Response model
public class ClaudeSdkResponse
{
    public bool Success { get; set; }
    public string Response { get; set; } = string.Empty;
    public JsonElement? ParsedJson { get; set; }
    public Dictionary<string, object>? Metadata { get; set; }
    public string? Error { get; set; }
}
```

---

### PHASE 4: Scraper Service

#### IScraperService.cs
```csharp
public interface IScraperService
{
    Task<ScrapingResult> ScrapeSeriesAsync(int mappingId);
    Task ProcessScrapingQueueAsync();
    Task<ScrapingResult> TestScraperAsync(int configId, string testUrl);
    Task DownloadChapterAsync(int resultId);
}

public class ScrapingResult
{
    public bool Success { get; set; }
    public int ChaptersFound { get; set; }
    public int NewChapters { get; set; }
    public string? Error { get; set; }
    public Dictionary<string, object>? Metadata { get; set; }
}
```

#### ScraperService.cs (`API/Services/Scraping/`)
```csharp
public class ScraperService : IScraperService
{
    private readonly IUnitOfWork _unitOfWork;
    private readonly IClaudeSdkClient _claudeClient;
    private readonly IHttpClientFactory _httpFactory;
    private readonly ILogger<ScraperService> _logger;
    private readonly IRateLimiter _rateLimiter;

    public ScraperService(
        IUnitOfWork unitOfWork,
        IClaudeSdkClient claudeClient,
        IHttpClientFactory httpFactory,
        ILogger<ScraperService> logger,
        IRateLimiter rateLimiter)
    {
        _unitOfWork = unitOfWork;
        _claudeClient = claudeClient;
        _httpFactory = httpFactory;
        _logger = logger;
        _rateLimiter = rateLimiter;
    }

    public async Task<ScrapingResult> ScrapeSeriesAsync(int mappingId)
    {
        var stopwatch = Stopwatch.StartNew();

        try
        {
            // 1. Load mapping with includes
            var mapping = await _unitOfWork.ScraperRepository.GetScraperMappingAsync(mappingId);
            if (mapping == null)
            {
                return new ScrapingResult { Success = false, Error = "Mapping not found" };
            }

            if (!mapping.IsActive || !mapping.ScraperConfiguration.IsEnabled)
            {
                _logger.LogInformation("[Scraper] Mapping {Id} is inactive, skipping", mappingId);
                return new ScrapingResult { Success = true, ChaptersFound = 0 };
            }

            _logger.LogInformation("[Scraper] Starting scrape for mapping {Id} - {Series}",
                mappingId, mapping.Series.Name);

            // 2. Rate limiting
            var domain = new Uri(mapping.ScraperConfiguration.BaseUrl).Host;
            await _rateLimiter.WaitForSlotAsync(
                domain,
                mapping.ScraperConfiguration.RateLimitPerMinute);

            // 3. Fetch HTML
            var url = mapping.ExternalUrl ??
                $"{mapping.ScraperConfiguration.BaseUrl}/series/{mapping.ExternalId}";

            var client = _httpFactory.CreateClient("Scraper");
            var html = await client.GetStringAsync(url);

            _logger.LogDebug("[Scraper] Fetched {Bytes} bytes from {Url}",
                html.Length, url);

            // 4. Extract data using Claude SDK
            var systemPrompt = @"You are a manga metadata extractor. Return ONLY valid JSON in this exact format:
{
  ""metadata"": {
    ""title"": string,
    ""author"": string,
    ""artist"": string,
    ""genres"": [string],
    ""description"": string,
    ""status"": ""ongoing"" | ""completed"" | ""hiatus"",
    ""year"": number,
    ""coverUrl"": string
  },
  ""chapters"": [
    {
      ""number"": string,
      ""title"": string,
      ""url"": string,
      ""releaseDate"": string,
      ""pageCount"": number
    }
  ]
}";

            var userPrompt = @"Extract the manga series metadata and complete chapter list from this HTML page.
For chapter numbers, use the actual chapter number (e.g., ""23"", ""23.5"").
For release dates, use ISO format if available.
Include ALL chapters found on the page.";

            var result = await _claudeClient.ExtractStructuredDataAsync(
                html,
                userPrompt,
                systemPrompt,
                model: "claude-haiku-4");

            if (!result.Success || result.ParsedJson == null)
            {
                throw new Exception($"Claude extraction failed: {result.Error}");
            }

            var data = result.ParsedJson.Value;

            // 5. Update series metadata (if not locked)
            if (!mapping.Series.Metadata.IsLocked)
            {
                var metadata = data.GetProperty("metadata");

                mapping.Series.Name = metadata.GetProperty("title").GetString()
                    ?? mapping.Series.Name;
                mapping.Series.Summary = metadata.GetProperty("description").GetString()
                    ?? mapping.Series.Summary;

                // Update other fields as needed
                // mapping.Series.Author = ...
                // mapping.Series.PublicationStatus = ...
            }

            // 6. Process chapters
            var chapters = data.GetProperty("chapters").EnumerateArray().ToList();
            var newChapters = 0;

            foreach (var chapter in chapters)
            {
                var chapterNum = chapter.GetProperty("number").GetString();
                if (string.IsNullOrEmpty(chapterNum)) continue;

                // Check if already scraped
                var existing = mapping.ScraperResults
                    .FirstOrDefault(r => r.ChapterNumber == chapterNum);

                if (existing != null) continue;

                // Create new result
                var chapterResult = new ScraperResult
                {
                    ScraperMappingId = mapping.Id,
                    ChapterNumber = chapterNum,
                    ChapterTitle = chapter.TryGetProperty("title", out var title)
                        ? title.GetString() : null,
                    ScrapedUrl = chapter.GetProperty("url").GetString() ?? "",
                    Status = ResultStatus.Pending,
                    ScrapedDate = DateTime.Now,
                    PageCount = chapter.TryGetProperty("pageCount", out var pc)
                        ? pc.GetInt32() : null
                };

                _unitOfWork.ScraperRepository.AddScraperResult(chapterResult);
                newChapters++;
            }

            // 7. Update mapping status
            mapping.LastScrapedDate = DateTime.Now;
            mapping.NextScheduledScrape = DateTime.Now.AddHours(24); // Next scrape in 24h
            mapping.FailureCount = 0;
            mapping.LastError = null;

            _unitOfWork.ScraperRepository.UpdateScraperMapping(mapping);

            // 8. Commit
            await _unitOfWork.CommitAsync();

            stopwatch.Stop();
            _logger.LogInformation(
                "[TIME] Scraped {Series} in {ElapsedMs}ms - Found {Total} chapters ({New} new)",
                mapping.Series.Name, stopwatch.ElapsedMilliseconds, chapters.Count, newChapters);

            return new ScrapingResult
            {
                Success = true,
                ChaptersFound = chapters.Count,
                NewChapters = newChapters
            };
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "[Scraper] Error scraping mapping {MappingId}", mappingId);

            // Update failure count
            var mapping = await _unitOfWork.ScraperRepository.GetScraperMappingAsync(mappingId);
            if (mapping != null)
            {
                mapping.FailureCount++;
                mapping.LastError = ex.Message;

                // Disable after 5 failures
                if (mapping.FailureCount >= 5)
                {
                    mapping.IsActive = false;
                    _logger.LogWarning("[Scraper] Disabled mapping {Id} after 5 failures", mappingId);
                }

                await _unitOfWork.CommitAsync();
            }

            return new ScrapingResult
            {
                Success = false,
                Error = ex.Message
            };
        }
    }

    public async Task ProcessScrapingQueueAsync()
    {
        var mappings = await _unitOfWork.ScraperRepository.GetDueForScrapingAsync();

        _logger.LogInformation("[Scraper] Processing queue - {Count} mappings due",
            mappings.Count());

        foreach (var mapping in mappings)
        {
            // Enqueue Hangfire job for each
            BackgroundJob.Enqueue(() => ScrapeSeriesAsync(mapping.Id));
        }
    }

    public async Task<ScrapingResult> TestScraperAsync(int configId, string testUrl)
    {
        // Similar to ScrapeSeriesAsync but doesn't save to DB
        // Returns extracted data for testing

        var config = await _unitOfWork.ScraperRepository
            .GetScraperConfigurationAsync(configId);

        if (config == null)
        {
            return new ScrapingResult { Success = false, Error = "Config not found" };
        }

        // Fetch HTML
        var client = _httpFactory.CreateClient("Scraper");
        var html = await client.GetStringAsync(testUrl);

        // Extract with Claude
        var result = await _claudeClient.ExtractStructuredDataAsync(
            html,
            "Extract manga metadata and chapters",
            config.ClaudePromptTemplate);

        return new ScrapingResult
        {
            Success = result.Success,
            Error = result.Error,
            Metadata = new Dictionary<string, object>
            {
                ["extractedData"] = result.ParsedJson ?? new object(),
                ["rawResponse"] = result.Response
            }
        };
    }

    public async Task DownloadChapterAsync(int resultId)
    {
        // Implement chapter download logic
        // 1. Get ScraperResult
        // 2. Fetch chapter pages
        // 3. Download images
        // 4. Create CBZ file
        // 5. Save to library
        // 6. Trigger Kavita scan

        throw new NotImplementedException("Chapter download to be implemented");
    }
}
```

---

### PHASE 5: Rate Limiter

#### IRateLimiter.cs
```csharp
public interface IRateLimiter
{
    Task WaitForSlotAsync(string key, int requestsPerMinute);
}

public class RateLimiter : IRateLimiter
{
    private readonly ConcurrentDictionary<string, Queue<DateTime>> _requestTimes;
    private readonly ConcurrentDictionary<string, SemaphoreSlim> _semaphores;

    public RateLimiter()
    {
        _requestTimes = new ConcurrentDictionary<string, Queue<DateTime>>();
        _semaphores = new ConcurrentDictionary<string, SemaphoreSlim>();
    }

    public async Task WaitForSlotAsync(string key, int requestsPerMinute)
    {
        var semaphore = _semaphores.GetOrAdd(key, _ => new SemaphoreSlim(1, 1));
        await semaphore.WaitAsync();

        try
        {
            var queue = _requestTimes.GetOrAdd(key, _ => new Queue<DateTime>());
            var now = DateTime.UtcNow;
            var oneMinuteAgo = now.AddMinutes(-1);

            // Remove old requests
            while (queue.Count > 0 && queue.Peek() < oneMinuteAgo)
            {
                queue.Dequeue();
            }

            // Wait if at limit
            if (queue.Count >= requestsPerMinute)
            {
                var waitTime = queue.Peek().AddMinutes(1) - now;
                if (waitTime.TotalMilliseconds > 0)
                {
                    await Task.Delay(waitTime);
                    queue.Dequeue();
                }
            }

            queue.Enqueue(now);
        }
        finally
        {
            semaphore.Release();
        }
    }
}
```

---

### PHASE 6: API Controllers & DTOs

#### DTOs (`API/DTOs/Scraping/`)
```csharp
public class ScraperConfigurationDto
{
    public int Id { get; set; }
    public string Name { get; set; } = string.Empty;
    public string SourceType { get; set; } = string.Empty;
    public string BaseUrl { get; set; } = string.Empty;
    public bool IsEnabled { get; set; }
    public int Priority { get; set; }
    public int RateLimitPerMinute { get; set; }
}

public class CreateScraperConfigurationDto
{
    [Required]
    public string Name { get; set; } = string.Empty;
    [Required]
    public ScraperSourceType SourceType { get; set; }
    [Required]
    [Url]
    public string BaseUrl { get; set; } = string.Empty;
    public int Priority { get; set; } = 5;
    public int RateLimitPerMinute { get; set; } = 30;
    public string? ClaudePromptTemplate { get; set; }
}

public class ScraperMappingDto
{
    public int Id { get; set; }
    public int SeriesId { get; set; }
    public string SeriesName { get; set; } = string.Empty;
    public int ScraperConfigurationId { get; set; }
    public string ExternalId { get; set; } = string.Empty;
    public string? ExternalUrl { get; set; }
    public DateTime? LastScrapedDate { get; set; }
    public bool IsActive { get; set; }
}

public class CreateScraperMappingDto
{
    [Required]
    public int SeriesId { get; set; }
    [Required]
    public int ScraperConfigurationId { get; set; }
    [Required]
    public string ExternalId { get; set; } = string.Empty;
    public string? ExternalUrl { get; set; }
}
```

#### ScraperController.cs (`API/Controllers/`)
```csharp
[Route("api/[controller]")]
[ApiController]
[Authorize]
public class ScraperController : ControllerBase
{
    private readonly IUnitOfWork _unitOfWork;
    private readonly IScraperService _scraperService;

    public ScraperController(IUnitOfWork unitOfWork, IScraperService scraperService)
    {
        _unitOfWork = unitOfWork;
        _scraperService = scraperService;
    }

    /// <summary>
    /// Get all scraper configurations
    /// </summary>
    [HttpGet("configurations")]
    [Authorize(Policy = "RequireAdminRole")]
    public async Task<ActionResult<IEnumerable<ScraperConfigurationDto>>> GetConfigurations()
    {
        var configs = await _unitOfWork.ScraperRepository.GetAllScraperConfigurationsAsync();
        // Map to DTOs
        return Ok(configs);
    }

    /// <summary>
    /// Create new scraper configuration
    /// </summary>
    [HttpPost("configurations")]
    [Authorize(Policy = "RequireAdminRole")]
    public async Task<ActionResult<ScraperConfigurationDto>> CreateConfiguration(
        CreateScraperConfigurationDto dto)
    {
        var config = new ScraperConfiguration
        {
            Name = dto.Name,
            SourceType = dto.SourceType,
            BaseUrl = dto.BaseUrl,
            Priority = dto.Priority,
            RateLimitPerMinute = dto.RateLimitPerMinute,
            ClaudePromptTemplate = dto.ClaudePromptTemplate ?? GetDefaultPromptTemplate()
        };

        _unitOfWork.ScraperRepository.AddScraperConfiguration(config);
        await _unitOfWork.CommitAsync();

        return CreatedAtAction(nameof(GetConfigurations), new { id = config.Id }, config);
    }

    /// <summary>
    /// Create mapping between series and external source
    /// </summary>
    [HttpPost("mappings")]
    [Authorize(Policy = "RequireAdminRole")]
    public async Task<ActionResult> CreateMapping(CreateScraperMappingDto dto)
    {
        var mapping = new ScraperMapping
        {
            SeriesId = dto.SeriesId,
            ScraperConfigurationId = dto.ScraperConfigurationId,
            ExternalId = dto.ExternalId,
            ExternalUrl = dto.ExternalUrl,
            IsActive = true
        };

        _unitOfWork.ScraperRepository.AddScraperMapping(mapping);
        await _unitOfWork.CommitAsync();

        // Trigger immediate scrape
        BackgroundJob.Enqueue(() => _scraperService.ScrapeSeriesAsync(mapping.Id));

        return Ok();
    }

    /// <summary>
    /// Manually trigger scrape for a mapping
    /// </summary>
    [HttpPost("mappings/{id}/scrape")]
    [Authorize(Policy = "RequireAdminRole")]
    public IActionResult TriggerScrape(int id)
    {
        BackgroundJob.Enqueue(() => _scraperService.ScrapeSeriesAsync(id));
        return Accepted();
    }

    /// <summary>
    /// Test scraper on a URL
    /// </summary>
    [HttpPost("configurations/{id}/test")]
    [Authorize(Policy = "RequireAdminRole")]
    public async Task<ActionResult> TestScraper(int id, [FromBody] TestScraperDto dto)
    {
        var result = await _scraperService.TestScraperAsync(id, dto.TestUrl);
        return Ok(result);
    }

    private string GetDefaultPromptTemplate()
    {
        return @"You are a manga metadata extractor. Return ONLY valid JSON in this format:
{
  ""metadata"": {""title"": string, ""author"": string, ""genres"": [string], ""description"": string},
  ""chapters"": [{""number"": string, ""title"": string, ""url"": string}]
}";
    }
}
```

---

### PHASE 7: Service Registration & Configuration

#### appsettings.json
```json
{
  "Scraper": {
    "ClaudeSdkUrl": "http://your-unraid-ip:8000",
    "RequestTimeout": 300,
    "RateLimitPerMinute": 30,
    "EnableAutoScraping": true,
    "ScrapingCron": "0 */6 * * *"
  }
}
```

#### Startup.cs or Program.cs
```csharp
// Register services
services.AddHttpClient("ClaudeSdk", client =>
{
    client.Timeout = TimeSpan.FromSeconds(300);
    client.DefaultRequestHeaders.Add("User-Agent", "Kavita-Scraper/1.0");
});

services.AddHttpClient("Scraper", client =>
{
    client.Timeout = TimeSpan.FromSeconds(30);
    client.DefaultRequestHeaders.Add("User-Agent", "Kavita-Scraper/1.0");
});

services.AddScoped<IClaudeSdkClient, ClaudeSdkClient>();
services.AddScoped<IScraperService, ScraperService>();
services.AddSingleton<IRateLimiter, RateLimiter>();
```

#### TaskScheduler.cs (add to existing)
```csharp
public void ScheduleScraperTasks()
{
    var cron = _config["Scraper:ScrapingCron"] ?? Cron.Hourly();

    RecurringJob.AddOrUpdate(
        "process-scraping-queue",
        () => ProcessScrapingQueueAsync(),
        cron);

    RecurringJob.AddOrUpdate(
        "cleanup-scraper-logs",
        () => CleanupScraperLogsAsync(),
        Cron.Daily);
}

[DisableConcurrentExecution]
public async Task ProcessScrapingQueueAsync()
{
    using var scope = _serviceProvider.CreateScope();
    var scraperService = scope.ServiceProvider.GetRequiredService<IScraperService>();
    await scraperService.ProcessScrapingQueueAsync();
}
```

---

## Key Differences from API Implementation

1. **No API Key Management** - Uses OAuth via Claude SDK
2. **HTTP Client Pattern** - Call SDK via HTTP POST, not direct API calls
3. **JSON Already Parsed** - SDK returns `parsed_json` field ready to use
4. **Flexible Prompting** - Can adjust prompts per-site without code changes
5. **Self-Hosted** - Runs on user's Unraid, no external API limits
6. **Cost Effective** - Uses Claude Code pricing model

## Testing Checklist

- [ ] Database migration creates all tables
- [ ] Can create ScraperConfiguration via API
- [ ] Can create ScraperMapping and triggers scrape
- [ ] Claude SDK is reachable from Kavita
- [ ] HTML is successfully sent to SDK
- [ ] JSON is parsed and chapters extracted
- [ ] Series metadata updates correctly
- [ ] ScraperResults are created for new chapters
- [ ] Background jobs execute on schedule
- [ ] Rate limiting works per-domain
- [ ] Error handling logs failures
- [ ] Failed mappings are disabled after 5 failures

## Success Criteria

When complete, users should be able to:
1. Add a manga source (MangaDex, etc.) as a ScraperConfiguration
2. Map their Kavita series to external manga sites
3. Automatically scrape for new chapters daily
4. View pending chapters in the UI
5. Download chapters with one click
6. All without maintaining CSS selectors or site-specific code

Claude figures out each site's structure automatically!
