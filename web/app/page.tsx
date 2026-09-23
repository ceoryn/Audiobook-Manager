'use client';

import { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  BookOpen,
  CheckCircle2,
  ChevronLeft,
  Database,
  Download,
  Folder,
  FolderOpen,
  HardDrive,
  LoaderCircle,
  Pause,
  Play,
  RotateCcw,
  Save,
  Settings2,
  ShieldCheck,
  Square,
  X,
} from 'lucide-react';
import { Button } from '@/components/ui/button';

type Problem = { book_id: string; state: string; failure?: string | null };
type Run = {
  id: number;
  status: string;
  counts: Record<string, number>;
  problems: Problem[];
};
type Status = {
  running: boolean;
  paused: boolean;
  configured: boolean;
  source: string | null;
  destination: string | null;
  run: Run | null;
};
type FolderEntry = {
  name: string;
  path: string;
  readable: boolean;
  writable: boolean;
};
type FolderListing = {
  path: string;
  parent: string | null;
  readable: boolean;
  writable: boolean;
  directories: FolderEntry[];
};
type PickerTarget = 'source' | 'destination';

const API = 'http://127.0.0.1:8788';

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    cache: 'no-store',
    ...init,
  });
  const payload = (await response.json()) as T & { error?: string };
  if (!response.ok) {
    throw new Error(
      payload.error || 'The local processing service rejected the request',
    );
  }
  return payload;
}

export default function Home() {
  const [data, setData] = useState<Status | null>(null);
  const [error, setError] = useState('');
  const [pendingControl, setPendingControl] = useState<
    'pause' | 'resume' | 'stop' | null
  >(null);
  const [source, setSource] = useState('');
  const [destination, setDestination] = useState('');
  const [pathsDirty, setPathsDirty] = useState(false);
  const [savingPaths, setSavingPaths] = useState(false);
  const [pickerTarget, setPickerTarget] = useState<PickerTarget | null>(null);
  const [folderListing, setFolderListing] = useState<FolderListing | null>(
    null,
  );
  const [folderLoading, setFolderLoading] = useState(false);
  const pathsInitialized = useRef(false);

  async function load() {
    try {
      const next = await requestJson<Status>('/api/process/status');
      setData(next);
      if (!pathsInitialized.current) {
        setSource(next.source ?? '');
        setDestination(next.destination ?? '');
        pathsInitialized.current = true;
      }
      if (!next.running) setPendingControl(null);
      setError('');
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : 'Could not load status',
      );
    }
  }

  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    const timer = window.setInterval(() => void load(), 3000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, []);

  function updatePath(target: PickerTarget, value: string) {
    if (target === 'source') setSource(value);
    else setDestination(value);
    setPathsDirty(true);
  }

  async function savePaths(): Promise<Status> {
    if (!source.trim() || !destination.trim()) {
      throw new Error('Choose both a source folder and an output folder');
    }
    setSavingPaths(true);
    try {
      const next = await requestJson<Status & { saved: boolean }>(
        '/api/config',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            source: source.trim(),
            destination: destination.trim(),
          }),
        },
      );
      setData(next);
      setSource(next.source ?? '');
      setDestination(next.destination ?? '');
      setPathsDirty(false);
      setError('');
      return next;
    } finally {
      setSavingPaths(false);
    }
  }

  async function start() {
    try {
      if (
        pathsDirty ||
        source.trim() !== (data?.source ?? '') ||
        destination.trim() !== (data?.destination ?? '')
      ) {
        await savePaths();
      }
      const next = await requestJson<Status & { started: boolean }>(
        '/api/process/start',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        },
      );
      setData(next);
      setError('');
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : 'Could not start processing',
      );
    }
  }

  async function control(action: 'pause' | 'resume' | 'stop') {
    if (pendingControl) return;
    setPendingControl(action);
    try {
      const next = await requestJson<Status & { changed: boolean }>(
        `/api/process/${action}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        },
      );
      setData(next);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : `Could not ${action} processing`,
      );
      setPendingControl(null);
    } finally {
      if (action !== 'stop') setPendingControl(null);
    }
  }

  async function browse(path?: string) {
    setFolderLoading(true);
    try {
      const query = path ? `?path=${encodeURIComponent(path)}` : '';
      setFolderListing(
        await requestJson<FolderListing>(`/api/folders${query}`),
      );
      setError('');
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : 'Could not browse that folder',
      );
    } finally {
      setFolderLoading(false);
    }
  }

  async function openPicker(target: PickerTarget) {
    setPickerTarget(target);
    setFolderListing(null);
    const current = target === 'source' ? source : destination;
    await browse(current.trim() || undefined);
  }

  function chooseCurrentFolder() {
    if (!pickerTarget || !folderListing) return;
    updatePath(pickerTarget, folderListing.path);
    setPickerTarget(null);
  }

  async function downloadLog() {
    try {
      const payload = await requestJson<object>('/api/process/log');
      const blob = new Blob([JSON.stringify(payload, null, 2)], {
        type: 'application/json',
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `audiobook-manager-diagnostic-${data?.run?.id ?? 'latest'}.json`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : 'Could not download the log',
      );
    }
  }

  const counts = data?.run?.counts ?? {};
  const discovered = Object.values(counts).reduce((a, b) => a + b, 0);
  const canStart = Boolean(
    data && source.trim() && destination.trim() && !data.running,
  );

  return (
    <main className="monitor-shell">
      <header className="monitor-head">
        <div className="brand">
          <BookOpen />
          <span>
            <small>AUDIOBOOK MANAGER</small>
            <b>Autonomous library engine</b>
          </span>
        </div>
        <div className={`live ${data?.running ? 'running' : ''}`}>
          <i />
          {data?.paused ? 'PAUSED' : data?.running ? 'PROCESSING' : 'READY'}
        </div>
      </header>

      <section className="hero">
        <div>
          <p className="eyebrow">HANDS-OFF WORKFLOW</p>
          <h1>
            {!data?.configured
              ? 'Choose your library folders'
              : data?.paused
                ? 'Processing paused safely'
                : data?.running
                  ? 'Building your clean library'
                  : data?.run?.status === 'complete'
                    ? 'Library job complete'
                    : 'Ready to organize your library'}
          </h1>
          <p>
            Detects books, resolves duplicate representations, identifies
            metadata, and creates verified Audiobookshelf-ready M4Bs.
          </p>
        </div>
        <Button
          className="start"
          disabled={!canStart}
          onClick={() => void start()}
        >
          {data?.running ? (
            <LoaderCircle className="spin" />
          ) : data?.run ? (
            <RotateCcw />
          ) : (
            <Play />
          )}
          {data?.running
            ? 'Processing…'
            : data?.run
              ? 'Run again'
              : 'Start processing'}
        </Button>
        {data?.running && (
          <div className="job-controls">
            <Button
              variant="outline"
              disabled={pendingControl !== null}
              onClick={() => void control(data.paused ? 'resume' : 'pause')}
            >
              {data.paused ? <Play /> : <Pause />}
              {data.paused ? 'Resume' : 'Pause'}
            </Button>
            <Button
              variant="outline"
              className="stop"
              disabled={pendingControl !== null}
              onClick={() => void control('stop')}
            >
              {pendingControl === 'stop' ? (
                <LoaderCircle className="spin" />
              ) : (
                <Square />
              )}
              {pendingControl === 'stop' ? 'Stopping safely…' : 'Stop'}
            </Button>
          </div>
        )}
      </section>

      <section className={`library-config ${data?.running ? 'is-locked' : ''}`}>
        <div className="config-heading">
          <span>
            <Settings2 />
            <span>
              <b>Library locations</b>
              <small>Saved only on this computer</small>
            </span>
          </span>
          {data?.running && <small>Locked while processing</small>}
        </div>
        <div className="path-grid">
          <label>
            <span>
              <Database />
              <span>
                <b>Source library</b>
                <small>READ ONLY</small>
              </span>
            </span>
            <div>
              <input
                value={source}
                disabled={data?.running}
                placeholder="/path/to/your/audiobooks"
                onChange={(event) => updatePath('source', event.target.value)}
              />
              <Button
                variant="outline"
                disabled={data?.running}
                onClick={() => void openPicker('source')}
              >
                <FolderOpen /> Browse
              </Button>
            </div>
          </label>
          <label>
            <span>
              <HardDrive />
              <span>
                <b>Clean output</b>
                <small>NEW FILES ONLY</small>
              </span>
            </span>
            <div>
              <input
                value={destination}
                disabled={data?.running}
                placeholder="/path/to/your/clean-library"
                onChange={(event) =>
                  updatePath('destination', event.target.value)
                }
              />
              <Button
                variant="outline"
                disabled={data?.running}
                onClick={() => void openPicker('destination')}
              >
                <FolderOpen /> Browse
              </Button>
            </div>
          </label>
        </div>
        <div className="config-footer">
          <span>
            <ShieldCheck /> Source and output must be separate, non-nested
            folders.
          </span>
          <Button
            variant="outline"
            disabled={Boolean(data?.running) || savingPaths || !pathsDirty}
            onClick={() =>
              void savePaths().catch((reason: unknown) => {
                setError(
                  reason instanceof Error
                    ? reason.message
                    : 'Could not save folders',
                );
              })
            }
          >
            {savingPaths ? <LoaderCircle className="spin" /> : <Save />}
            {savingPaths
              ? 'Saving…'
              : pathsDirty
                ? 'Save locations'
                : 'Locations saved'}
          </Button>
        </div>
      </section>

      <section className="monitor-metrics">
        <article>
          <small>BOOKS DISCOVERED</small>
          <b>{discovered}</b>
        </article>
        <article>
          <small>COMPLETED</small>
          <b className="green">{counts.complete ?? 0}</b>
        </article>
        <article>
          <small>READY TO BUILD</small>
          <b>{(counts.metadata_matched ?? 0) + (counts.local_metadata ?? 0)}</b>
        </article>
        <article>
          <small>QUARANTINED</small>
          <b className="amber">{counts.quarantined ?? 0}</b>
        </article>
        <article>
          <small>FAILED</small>
          <b className="red">{counts.failed ?? 0}</b>
        </article>
      </section>

      <section className="activity">
        <div className="activity-title">
          <div>
            <p className="eyebrow">JOB STATUS</p>
            <h2>
              {data?.run
                ? `Run #${data.run.id} · ${data.run.status}`
                : 'No processing run yet'}
            </h2>
          </div>
          <div className="activity-actions">
            <Button variant="outline" onClick={() => void downloadLog()}>
              <Download /> Download diagnostic log
            </Button>
            {data?.running && (
              <span className="update-status">
                <i />
                <span>
                  <b>Live updates</b>
                  <small>Refreshes every 3 seconds</small>
                </span>
              </span>
            )}
          </div>
        </div>
        {error ? (
          <div className="notice error">
            <AlertTriangle /> {error}
          </div>
        ) : data?.run?.status === 'complete' ? (
          <div className="notice success">
            <CheckCircle2 />
            Processing finished. Verified books are in the destination;
            questionable items are recorded in _quarantine.
          </div>
        ) : (
          <div className="notice">
            <ShieldCheck />
            Source files are never modified. One questionable book cannot stop
            the rest of the job.
          </div>
        )}
        {data?.running ? (
          <div className="problems-refreshing">
            <LoaderCircle className="spin" />
            Previous quarantine entries are hidden while the library is being
            reevaluated.
          </div>
        ) : (
          <details className="problems">
            <summary>
              Quarantine &amp; failures
              <small>{data?.run?.problems?.length ?? 0} entries · Show</small>
            </summary>
            {data?.run?.problems?.length ? (
              data.run.problems.map((problem) => (
                <article key={problem.book_id}>
                  <AlertTriangle />
                  <span>
                    <b>{problem.book_id}</b>
                    <small>{problem.failure || problem.state}</small>
                  </span>
                </article>
              ))
            ) : (
              <p>No problems recorded for this run.</p>
            )}
          </details>
        )}
      </section>

      {pickerTarget && (
        <div className="folder-modal-backdrop" role="presentation">
          <dialog className="folder-modal" open>
            <header>
              <span>
                <FolderOpen />
                <span>
                  <b>
                    Choose{' '}
                    {pickerTarget === 'source'
                      ? 'source library'
                      : 'output folder'}
                  </b>
                  <small>Folders on this computer</small>
                </span>
              </span>
              <button
                aria-label="Close folder browser"
                onClick={() => setPickerTarget(null)}
              >
                <X />
              </button>
            </header>
            <div className="folder-location">
              <code>{folderListing?.path ?? 'Loading…'}</code>
            </div>
            <div className="folder-list">
              {folderListing?.parent && (
                <button
                  onClick={() => void browse(folderListing.parent ?? undefined)}
                >
                  <ChevronLeft />
                  <span>
                    <b>Parent folder</b>
                    <small>{folderListing.parent}</small>
                  </span>
                </button>
              )}
              {folderLoading ? (
                <div className="folder-loading">
                  <LoaderCircle className="spin" /> Loading folders…
                </div>
              ) : folderListing?.directories.length ? (
                folderListing.directories.map((entry) => (
                  <button
                    key={entry.path}
                    disabled={!entry.readable}
                    onClick={() => void browse(entry.path)}
                  >
                    <Folder />
                    <span>
                      <b>{entry.name}</b>
                      <small>{entry.path}</small>
                    </span>
                  </button>
                ))
              ) : (
                <div className="folder-loading">No visible subfolders</div>
              )}
            </div>
            <footer>
              <span>
                {pickerTarget === 'destination' &&
                folderListing &&
                !folderListing.writable
                  ? 'This folder is not writable.'
                  : 'Select the folder shown above.'}
              </span>
              <Button variant="outline" onClick={() => setPickerTarget(null)}>
                Cancel
              </Button>
              <Button
                disabled={
                  !folderListing ||
                  !folderListing.readable ||
                  (pickerTarget === 'destination' && !folderListing.writable)
                }
                onClick={chooseCurrentFolder}
              >
                <CheckCircle2 /> Use this folder
              </Button>
            </footer>
          </dialog>
        </div>
      )}
    </main>
  );
}
