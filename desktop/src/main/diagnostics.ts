/** Explicit, cached shell samples. No timer, disk journal, URLs or command lines. */
import type { ProcessMetric } from 'electron';
import { cpus } from 'node:os';

export function desktopSampler(read: () => ProcessMetric[], clock = () => performance.now(), cpuCount = cpus().length || 1) {
	let cached: ReturnType<typeof sample> | undefined;
	let previous = new Map<string, number>();
	let previousTime: number | undefined;
	function sample() {
		const now = clock();
		const started = process.cpuUsage();
		const all = read();
		const next = new Map<string, number>();
		const processes = all.slice(0, 256).map(p => {
			const key = `${p.pid}:${p.creationTime}`;
			const ticks = p.cpu.cumulativeCPUUsage;
			const old = previous.get(key);
			if (ticks !== undefined) next.set(key, ticks);
			return {
				pid: p.pid, started_at: p.creationTime / 1000,
				role: p.type === 'Browser' ? 'desktop_main' : p.type === 'Tab' ? 'desktop_renderer' : p.type === 'GPU' ? 'desktop_gpu_process_cpu' : 'desktop_other',
				cpu_pct_machine: ticks !== undefined && old !== undefined && previousTime !== undefined && now > previousTime
					? Math.max(0, ticks - old) / ((now - previousTime) / 1000) / cpuCount * 100 : null,
				working_set_mib: p.memory.workingSetSize / 1024,
				// Electron reports privateBytes only on Windows; do not call it unique RAM.
				electron_private_mib: p.memory.privateBytes === undefined ? null : p.memory.privateBytes / 1024
			};
		});
		const interval = previousTime === undefined ? null : (now - previousTime) / 1000;
		previous = next; // Exited identities are discarded; no lifecycle history accumulates.
		previousTime = now;
		const cost = process.cpuUsage(started);
		return {
			scope: 'electron_only', sampled_monotonic_ms: now, cpu_interval_seconds: interval,
			logical_cpus: cpuCount, processes, omitted_processes: Math.max(0, all.length - processes.length),
			working_set_is_unique_ram: false, gpu_engine_utilization_measured: false,
			collector_cpu_seconds: (cost.user + cost.system) / 1e6,
			cpu_includes_collector: true, collection_ms: clock() - now
		};
	}
	return () => {
		if (!cached || clock() - cached.sampled_monotonic_ms >= 5000) cached = sample();
		return cached;
	};
}
