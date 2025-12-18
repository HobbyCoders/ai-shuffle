<script lang="ts">
    import { git, commits, gitLoading, gitStatus } from '$lib/stores/git';
    import type { CommitNode } from '$lib/stores/git';

    interface Props {
        projectId: string | null;
    }

    let { projectId }: Props = $props();

    let selectedCommit = $state<CommitNode | null>(null);
    let graphLimit = $state(50);

    // Layout constants
    const NODE_RADIUS = 6;
    const ROW_HEIGHT = 40;
    const COL_WIDTH = 24;
    const LEFT_PADDING = 20;
    const TOP_PADDING = 20;

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

    // Calculate graph layout
    function calculateLayout(commits: CommitNode[]): {
        nodes: (CommitNode & { x: number; y: number; color: string })[];
        edges: { x1: number; y1: number; x2: number; y2: number; color: string }[];
        width: number;
        height: number;
    } {
        if (!commits.length) {
            return { nodes: [], edges: [], width: 0, height: 0 };
        }

        const commitMap = new Map<string, CommitNode & { x: number; y: number; color: string }>();
        const columnUsage = new Map<number, string>(); // column -> commit sha using it
        const branchColumns = new Map<string, number>(); // branch/sha -> column
        const nodes: (CommitNode & { x: number; y: number; color: string })[] = [];
        const edges: { x1: number; y1: number; x2: number; y2: number; color: string }[] = [];

        let maxColumn = 0;

        // First pass: assign columns based on parent relationships
        commits.forEach((commit, index) => {
            let column = 0;

            // Try to use parent's column if available and not in use
            if (commit.parents.length > 0) {
                const parent = commitMap.get(commit.parents[0]);
                if (parent) {
                    // If parent's column is free at this row, use it
                    const currentUser = columnUsage.get(parent.x);
                    if (!currentUser || currentUser === commit.parents[0]) {
                        column = parent.x;
                    }
                }
            }

            // If column is in use by another branch, find next available
            while (columnUsage.has(column) && columnUsage.get(column) !== commit.sha) {
                const user = columnUsage.get(column);
                // Check if the column user is our parent
                if (user && commit.parents.includes(user)) {
                    break;
                }
                column++;
            }

            const color = BRANCH_COLORS[column % BRANCH_COLORS.length];
            const node = {
                ...commit,
                x: column,
                y: index,
                color
            };

            commitMap.set(commit.sha, node);
            columnUsage.set(column, commit.sha);
            maxColumn = Math.max(maxColumn, column);

            // Clear column if this commit continues the line
            if (commit.parents.length > 0) {
                const firstParent = commitMap.get(commit.parents[0]);
                if (firstParent && firstParent.x !== column) {
                    // Branch merged or diverged, clear parent column
                    columnUsage.delete(firstParent.x);
                }
            }

            nodes.push(node);
        });

        // Second pass: create edges
        nodes.forEach(node => {
            node.parents.forEach((parentSha, parentIndex) => {
                const parent = commitMap.get(parentSha);
                if (parent) {
                    const x1 = LEFT_PADDING + node.x * COL_WIDTH;
                    const y1 = TOP_PADDING + node.y * ROW_HEIGHT;
                    const x2 = LEFT_PADDING + parent.x * COL_WIDTH;
                    const y2 = TOP_PADDING + parent.y * ROW_HEIGHT;

                    // Use parent's color for the edge
                    edges.push({
                        x1,
                        y1,
                        x2,
                        y2,
                        color: parentIndex === 0 ? node.color : parent.color
                    });
                }
            });
        });

        // Calculate final positions
        nodes.forEach(node => {
            node.x = LEFT_PADDING + node.x * COL_WIDTH;
            node.y = TOP_PADDING + node.y * ROW_HEIGHT;
        });

        return {
            nodes,
            edges,
            width: LEFT_PADDING * 2 + (maxColumn + 1) * COL_WIDTH,
            height: TOP_PADDING * 2 + commits.length * ROW_HEIGHT
        };
    }

    // Generate edge path (bezier curve for merges)
    function getEdgePath(edge: { x1: number; y1: number; x2: number; y2: number }): string {
        if (edge.x1 === edge.x2) {
            // Straight line
            return `M ${edge.x1} ${edge.y1} L ${edge.x2} ${edge.y2}`;
        } else {
            // Bezier curve for branch/merge
            const midY = (edge.y1 + edge.y2) / 2;
            return `M ${edge.x1} ${edge.y1} C ${edge.x1} ${midY} ${edge.x2} ${midY} ${edge.x2} ${edge.y2}`;
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

    // Reactive layout calculation
    const layout = $derived(calculateLayout($commits));
</script>

<div class="p-4">
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
        <div class="flex flex-col lg:flex-row gap-4">
            <!-- Graph visualization -->
            <div class="flex-1 overflow-x-auto border border-border rounded-lg bg-card">
                <div class="min-w-[600px]" style="height: {Math.min(layout.height + 40, 500)}px; overflow-y: auto;">
                    <svg
                        width="100%"
                        height={layout.height}
                        class="block"
                    >
                        <!-- Edges -->
                        {#each layout.edges as edge}
                            <path
                                d={getEdgePath(edge)}
                                fill="none"
                                stroke={edge.color}
                                stroke-width="2"
                                stroke-linecap="round"
                            />
                        {/each}

                        <!-- Nodes -->
                        {#each layout.nodes as node, index}
                            {@const isHead = node.branches?.includes('HEAD') || (index === 0 && node.branches?.includes($gitStatus?.current_branch || ''))}
                            <g
                                transform="translate({node.x}, {node.y})"
                                class="cursor-pointer"
                                onclick={() => selectedCommit = node}
                            >
                                <!-- Node circle -->
                                <circle
                                    r={NODE_RADIUS}
                                    fill={isHead ? 'var(--primary)' : node.color}
                                    stroke={selectedCommit?.sha === node.sha ? 'var(--foreground)' : 'transparent'}
                                    stroke-width="2"
                                />

                                <!-- HEAD indicator -->
                                {#if isHead}
                                    <circle
                                        r={NODE_RADIUS + 4}
                                        fill="none"
                                        stroke="var(--primary)"
                                        stroke-width="2"
                                        stroke-dasharray="4 2"
                                    />
                                {/if}

                                <!-- Commit info -->
                                <g transform="translate(20, 0)">
                                    <!-- Short SHA -->
                                    <text
                                        y="4"
                                        class="text-xs fill-current text-muted-foreground font-mono"
                                        style="font-size: 11px;"
                                    >
                                        {node.shortSha}
                                    </text>

                                    <!-- Commit message -->
                                    <text
                                        x="60"
                                        y="4"
                                        class="text-sm fill-current text-foreground"
                                        style="font-size: 12px;"
                                    >
                                        {node.message.length > 50 ? node.message.substring(0, 50) + '...' : node.message}
                                    </text>

                                    <!-- Branch labels -->
                                    {#each node.branches || [] as branch, i}
                                        <rect
                                            x={350 + i * 80}
                                            y="-8"
                                            rx="3"
                                            ry="3"
                                            width={branch.length * 7 + 12}
                                            height="16"
                                            fill="var(--primary)"
                                            opacity="0.15"
                                        />
                                        <text
                                            x={356 + i * 80}
                                            y="3"
                                            class="fill-current text-primary"
                                            style="font-size: 10px; font-weight: 500;"
                                        >
                                            {branch}
                                        </text>
                                    {/each}

                                    <!-- Tags -->
                                    {#each node.tags || [] as tag, i}
                                        <rect
                                            x={350 + (node.branches?.length || 0) * 80 + i * 70}
                                            y="-8"
                                            rx="3"
                                            ry="3"
                                            width={tag.length * 7 + 12}
                                            height="16"
                                            fill="var(--chart-4)"
                                            opacity="0.15"
                                        />
                                        <text
                                            x={356 + (node.branches?.length || 0) * 80 + i * 70}
                                            y="3"
                                            style="font-size: 10px; font-weight: 500; fill: var(--chart-4);"
                                        >
                                            {tag}
                                        </text>
                                    {/each}
                                </g>
                            </g>
                        {/each}
                    </svg>
                </div>

                <!-- Load more button -->
                {#if $commits.length >= graphLimit}
                    <div class="p-3 border-t border-border text-center">
                        <button
                            onclick={handleLoadMore}
                            disabled={$gitLoading}
                            class="text-sm text-primary hover:underline disabled:opacity-50"
                        >
                            Load more commits...
                        </button>
                    </div>
                {/if}
            </div>

            <!-- Commit details panel -->
            {#if selectedCommit}
                <div class="w-full lg:w-80 border border-border rounded-lg bg-card p-4">
                    <div class="flex items-center justify-between mb-4">
                        <h3 class="font-medium text-foreground">Commit Details</h3>
                        <button
                            onclick={() => selectedCommit = null}
                            class="p-1 rounded hover:bg-muted text-muted-foreground hover:text-foreground"
                        >
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                            </svg>
                        </button>
                    </div>

                    <div class="space-y-3">
                        <!-- SHA -->
                        <div>
                            <label class="text-xs text-muted-foreground uppercase tracking-wide">SHA</label>
                            <p class="font-mono text-sm text-foreground break-all">{selectedCommit.sha}</p>
                        </div>

                        <!-- Message -->
                        <div>
                            <label class="text-xs text-muted-foreground uppercase tracking-wide">Message</label>
                            <p class="text-sm text-foreground">{selectedCommit.message}</p>
                        </div>

                        <!-- Author -->
                        <div>
                            <label class="text-xs text-muted-foreground uppercase tracking-wide">Author</label>
                            <p class="text-sm text-foreground">{selectedCommit.author}</p>
                            <p class="text-xs text-muted-foreground">{selectedCommit.email}</p>
                        </div>

                        <!-- Date -->
                        <div>
                            <label class="text-xs text-muted-foreground uppercase tracking-wide">Date</label>
                            <p class="text-sm text-foreground">{formatDate(selectedCommit.date)}</p>
                            <p class="text-xs text-muted-foreground">{new Date(selectedCommit.date).toLocaleString()}</p>
                        </div>

                        <!-- Parents -->
                        {#if selectedCommit.parents.length > 0}
                            <div>
                                <label class="text-xs text-muted-foreground uppercase tracking-wide">Parents</label>
                                <div class="space-y-1 mt-1">
                                    {#each selectedCommit.parents as parent}
                                        <p class="font-mono text-xs text-muted-foreground">{parent.substring(0, 7)}</p>
                                    {/each}
                                </div>
                            </div>
                        {/if}

                        <!-- Branches -->
                        {#if selectedCommit.branches && selectedCommit.branches.length > 0}
                            <div>
                                <label class="text-xs text-muted-foreground uppercase tracking-wide">Branches</label>
                                <div class="flex flex-wrap gap-1 mt-1">
                                    {#each selectedCommit.branches as branch}
                                        <span class="px-2 py-0.5 text-xs rounded-full bg-primary/15 text-primary">{branch}</span>
                                    {/each}
                                </div>
                            </div>
                        {/if}

                        <!-- Tags -->
                        {#if selectedCommit.tags && selectedCommit.tags.length > 0}
                            <div>
                                <label class="text-xs text-muted-foreground uppercase tracking-wide">Tags</label>
                                <div class="flex flex-wrap gap-1 mt-1">
                                    {#each selectedCommit.tags as tag}
                                        <span class="px-2 py-0.5 text-xs rounded-full bg-amber-500/15 text-amber-600">{tag}</span>
                                    {/each}
                                </div>
                            </div>
                        {/if}
                    </div>

                    <!-- Actions -->
                    <div class="mt-4 pt-4 border-t border-border flex gap-2">
                        <button
                            onclick={() => navigator.clipboard.writeText(selectedCommit?.sha || '')}
                            class="flex-1 px-3 py-2 text-xs bg-muted text-foreground rounded-lg hover:bg-accent transition-colors"
                        >
                            Copy SHA
                        </button>
                        <button
                            onclick={() => {
                                if (selectedCommit) {
                                    git.checkout(selectedCommit.sha);
                                }
                            }}
                            class="flex-1 px-3 py-2 text-xs bg-primary text-primary-foreground rounded-lg hover:opacity-90 transition-opacity"
                        >
                            Checkout
                        </button>
                    </div>
                </div>
            {/if}
        </div>
    {/if}
</div>
