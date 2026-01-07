<script lang="ts">
    import { git, commits, gitLoading, gitStatus } from '$lib/stores/git';
    import type { CommitNode } from '$lib/stores/git';

    interface Props {
        projectId: string | null;
    }

    let { projectId }: Props = $props();

    let selectedCommit = $state<CommitNode | null>(null);
    let graphLimit = $state(50);

    // Layout constants for horizontal timeline
    const NODE_RADIUS = 8;
    const COL_WIDTH = 120;  // Horizontal spacing between commits
    const ROW_HEIGHT = 28;  // Vertical spacing between branch lanes
    const LEFT_PADDING = 40;
    const TOP_PADDING = 60;

    // Branch colors (consistent colors for branch lanes)
    const BRANCH_COLORS = [
        '#3b82f6', // blue
        '#22c55e', // green
        '#f59e0b', // amber
        '#ef4444', // red
        '#a855f7', // purple
        '#06b6d4', // cyan
        '#ec4899', // pink
        '#f97316', // orange
    ];

    // Calculate horizontal graph layout (left to right, newest on left)
    // Simple linear layout - all commits on same horizontal line
    function calculateLayout(commits: CommitNode[]): {
        nodes: (CommitNode & { x: number; y: number; color: string; lane: number })[];
        edges: { x1: number; y1: number; x2: number; y2: number; color: string }[];
        width: number;
        height: number;
    } {
        if (!commits.length) {
            return { nodes: [], edges: [], width: 0, height: 0 };
        }

        const commitMap = new Map<string, CommitNode & { x: number; y: number; color: string; lane: number }>();
        const nodes: (CommitNode & { x: number; y: number; color: string; lane: number })[] = [];
        const edges: { x1: number; y1: number; x2: number; y2: number; color: string }[] = [];

        // Simple layout: all commits on lane 0 (single horizontal line)
        // Each commit gets its own x position based on index
        commits.forEach((commit, index) => {
            const color = BRANCH_COLORS[0]; // Use primary color for main line
            const node = {
                ...commit,
                x: LEFT_PADDING + index * COL_WIDTH,  // Horizontal position based on index
                y: TOP_PADDING,                        // All on same horizontal line
                color,
                lane: 0
            };

            commitMap.set(commit.sha, node);
            nodes.push(node);
        });

        // Create edges between commits and their parents
        nodes.forEach(node => {
            node.parents.forEach((parentSha) => {
                const parent = commitMap.get(parentSha);
                if (parent) {
                    edges.push({
                        x1: node.x,
                        y1: node.y,
                        x2: parent.x,
                        y2: parent.y,
                        color: node.color
                    });
                }
            });
        });

        return {
            nodes,
            edges,
            width: LEFT_PADDING * 2 + commits.length * COL_WIDTH,
            height: TOP_PADDING * 2 + 80  // Fixed height for single lane + labels
        };
    }

    // Generate edge path (bezier curve for merges) - horizontal version
    function getEdgePath(edge: { x1: number; y1: number; x2: number; y2: number }): string {
        if (edge.y1 === edge.y2) {
            // Straight horizontal line
            return `M ${edge.x1} ${edge.y1} L ${edge.x2} ${edge.y2}`;
        } else {
            // Bezier curve for branch/merge
            const midX = (edge.x1 + edge.x2) / 2;
            return `M ${edge.x1} ${edge.y1} C ${midX} ${edge.y1} ${midX} ${edge.y2} ${edge.x2} ${edge.y2}`;
        }
    }

    function handleLoadMore() {
        graphLimit += 50;
        if (projectId) {
            git.loadGraph(graphLimit);
        }
    }

    function formatDate(dateStr: string): string {
        const date = new Date(dateStr);
        const now = new Date();
        const diff = now.getTime() - date.getTime();
        const days = Math.floor(diff / (1000 * 60 * 60 * 24));

        if (days === 0) {
            const hours = Math.floor(diff / (1000 * 60 * 60));
            if (hours === 0) {
                const mins = Math.floor(diff / (1000 * 60));
                return `${mins}m ago`;
            }
            return `${hours}h ago`;
        } else if (days < 7) {
            return `${days}d ago`;
        } else {
            return date.toLocaleDateString();
        }
    }

    function formatShortDate(dateStr: string): string {
        const date = new Date(dateStr);
        return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    }

    // Reactive layout calculation
    const layout = $derived(calculateLayout($commits));
</script>

<div class="flex flex-col h-full">
    {#if $gitLoading && $commits.length === 0}
        <div class="flex items-center justify-center py-12">
            <svg class="w-6 h-6 animate-spin text-primary" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            <span class="ml-2 text-muted-foreground">Loading commit history...</span>
        </div>
    {:else if $commits.length === 0}
        <div class="text-center py-12 text-muted-foreground">
            <svg class="w-12 h-12 mx-auto mb-3 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
            </svg>
            <p>No commits found</p>
            <p class="text-sm mt-1">This repository may be empty or not initialized</p>
        </div>
    {:else}
        <!-- Horizontal scrolling timeline container -->
        <div class="flex-1 overflow-x-auto overflow-y-hidden border border-border rounded-lg bg-card">
            <div style="width: {layout.width}px; min-width: 100%; height: {Math.max(layout.height, 200)}px;" class="relative">
                <svg
                    width={layout.width}
                    height={layout.height}
                    class="block"
                >
                    <!-- Timeline base line -->
                    <line
                        x1={LEFT_PADDING - 10}
                        y1={TOP_PADDING}
                        x2={layout.width - LEFT_PADDING + 10}
                        y2={TOP_PADDING}
                        stroke="var(--border)"
                        stroke-width="2"
                        stroke-dasharray="4 4"
                    />

                    <!-- Edges (connections between commits) -->
                    {#each layout.edges as edge}
                        <path
                            d={getEdgePath(edge)}
                            fill="none"
                            stroke={edge.color}
                            stroke-width="2"
                            stroke-linecap="round"
                        />
                    {/each}

                    <!-- Nodes (commits) -->
                    {#each layout.nodes as node, index}
                        {@const isHead = node.branches?.includes('HEAD') || (index === 0 && node.branches?.includes($gitStatus?.current_branch || ''))}
                        {@const isSelected = selectedCommit?.sha === node.sha}
                        <g
                            transform="translate({node.x}, {node.y})"
                            class="cursor-pointer group"
                            onclick={() => selectedCommit = selectedCommit?.sha === node.sha ? null : node}
                            role="button"
                            tabindex="0"
                            onkeydown={(e) => e.key === 'Enter' && (selectedCommit = selectedCommit?.sha === node.sha ? null : node)}
                        >
                            <!-- Node circle with hover effect -->
                            <circle
                                r={isSelected ? NODE_RADIUS + 2 : NODE_RADIUS}
                                fill={isHead ? 'var(--primary)' : node.color}
                                stroke={isSelected ? 'var(--foreground)' : 'var(--card)'}
                                stroke-width={isSelected ? 3 : 2}
                                class="transition-all duration-150"
                            />

                            <!-- HEAD indicator ring -->
                            {#if isHead}
                                <circle
                                    r={NODE_RADIUS + 6}
                                    fill="none"
                                    stroke="var(--primary)"
                                    stroke-width="2"
                                    stroke-dasharray="4 2"
                                    class="animate-pulse"
                                />
                            {/if}

                            <!-- Branch labels above node -->
                            {#each node.branches || [] as branch, i}
                                <g transform="translate(0, {-NODE_RADIUS - 12 - i * 20})">
                                    <rect
                                        x={-branch.length * 3.5 - 6}
                                        y="-8"
                                        rx="4"
                                        ry="4"
                                        width={branch.length * 7 + 12}
                                        height="16"
                                        fill="var(--primary)"
                                        opacity="0.9"
                                    />
                                    <text
                                        y="3"
                                        text-anchor="middle"
                                        class="fill-primary-foreground"
                                        style="font-size: 10px; font-weight: 600;"
                                    >
                                        {branch}
                                    </text>
                                </g>
                            {/each}

                            <!-- Tag labels -->
                            {#each node.tags || [] as tag, i}
                                <g transform="translate(0, {-NODE_RADIUS - 12 - (node.branches?.length || 0) * 20 - i * 20})">
                                    <rect
                                        x={-tag.length * 3.5 - 6}
                                        y="-8"
                                        rx="4"
                                        ry="4"
                                        width={tag.length * 7 + 12}
                                        height="16"
                                        fill="var(--chart-4)"
                                        opacity="0.9"
                                    />
                                    <text
                                        y="3"
                                        text-anchor="middle"
                                        style="font-size: 10px; font-weight: 600; fill: white;"
                                    >
                                        {tag}
                                    </text>
                                </g>
                            {/each}

                            <!-- Short SHA below node -->
                            <text
                                y={NODE_RADIUS + 16}
                                text-anchor="middle"
                                class="fill-current text-muted-foreground font-mono"
                                style="font-size: 10px;"
                            >
                                {node.shortSha}
                            </text>

                            <!-- Date below SHA -->
                            <text
                                y={NODE_RADIUS + 28}
                                text-anchor="middle"
                                class="fill-current text-muted-foreground/70"
                                style="font-size: 9px;"
                            >
                                {formatShortDate(node.date)}
                            </text>
                        </g>
                    {/each}

                    <!-- "Now" label on the left -->
                    <text
                        x={LEFT_PADDING - 20}
                        y={TOP_PADDING + 4}
                        text-anchor="end"
                        class="fill-current text-muted-foreground"
                        style="font-size: 10px; font-weight: 500;"
                    >
                        NOW
                    </text>

                    <!-- "Older" arrow on the right -->
                    <g transform="translate({layout.width - LEFT_PADDING + 20}, {TOP_PADDING})">
                        <text
                            x="-5"
                            y="4"
                            text-anchor="end"
                            class="fill-current text-muted-foreground"
                            style="font-size: 10px; font-weight: 500;"
                        >
                            OLDER
                        </text>
                        <path
                            d="M 0 0 L 10 0 L 6 -4 M 10 0 L 6 4"
                            fill="none"
                            stroke="var(--muted-foreground)"
                            stroke-width="1.5"
                            stroke-linecap="round"
                            stroke-linejoin="round"
                        />
                    </g>
                </svg>
            </div>

            <!-- Load more button (at the end) -->
            {#if $commits.length >= graphLimit}
                <div class="absolute right-4 top-1/2 -translate-y-1/2">
                    <button
                        onclick={handleLoadMore}
                        disabled={$gitLoading}
                        class="px-3 py-2 text-xs bg-muted text-foreground rounded-lg hover:bg-accent transition-colors disabled:opacity-50 shadow-lg"
                    >
                        Load more →
                    </button>
                </div>
            {/if}
        </div>

        <!-- Commit details panel (below timeline) -->
        {#if selectedCommit}
            <div class="mt-4 border border-border rounded-lg bg-card p-4">
                <div class="flex items-start justify-between gap-4">
                    <div class="flex-1 min-w-0">
                        <div class="flex items-center gap-3 mb-2">
                            <span class="font-mono text-sm text-primary bg-primary/10 px-2 py-0.5 rounded">{selectedCommit.shortSha}</span>
                            {#each selectedCommit.branches || [] as branch}
                                <span class="px-2 py-0.5 text-xs rounded-full bg-primary/15 text-primary">{branch}</span>
                            {/each}
                            {#each selectedCommit.tags || [] as tag}
                                <span class="px-2 py-0.5 text-xs rounded-full bg-amber-500/15 text-amber-600">{tag}</span>
                            {/each}
                        </div>
                        <p class="text-foreground font-medium mb-1">{selectedCommit.message}</p>
                        <div class="flex items-center gap-4 text-xs text-muted-foreground">
                            <span>{selectedCommit.author}</span>
                            <span>{formatDate(selectedCommit.date)}</span>
                            {#if selectedCommit.parents.length > 0}
                                <span>Parent: {selectedCommit.parents[0].substring(0, 7)}</span>
                            {/if}
                        </div>
                    </div>
                    <div class="flex items-center gap-2 shrink-0">
                        <button
                            onclick={() => navigator.clipboard.writeText(selectedCommit?.sha || '')}
                            class="px-3 py-1.5 text-xs bg-muted text-foreground rounded-lg hover:bg-accent transition-colors"
                            title="Copy full SHA"
                        >
                            Copy SHA
                        </button>
                        <button
                            onclick={() => {
                                if (selectedCommit) {
                                    git.checkout(selectedCommit.sha);
                                }
                            }}
                            class="px-3 py-1.5 text-xs bg-primary text-primary-foreground rounded-lg hover:opacity-90 transition-opacity"
                        >
                            Checkout
                        </button>
                        <button
                            onclick={() => selectedCommit = null}
                            class="p-1.5 rounded hover:bg-muted text-muted-foreground hover:text-foreground"
                            title="Close"
                        >
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                            </svg>
                        </button>
                    </div>
                </div>
            </div>
        {/if}
    {/if}
</div>
