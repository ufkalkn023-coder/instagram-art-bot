"""Build or serve a local editorial workspace without publishing."""
import argparse
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.editorial_workspace import create_editorial_workspace, load_workspace


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--projects', nargs='+', type=Path, required=True)
    create.add_argument('--output', type=Path, required=True)
    create.add_argument('--history-project', action='append', type=Path, default=[])
    create.add_argument('--dossier', type=Path)
    serve = commands.add_parser('serve')
    serve.add_argument('--workspace', type=Path, required=True)
    serve.add_argument('--port', type=int, default=38141)
    args = parser.parse_args(argv)
    try:
        if args.command == 'serve':
            from src.story_editor import editor_server
            value = load_workspace(args.workspace)
            server = editor_server(Path(value['projects'][0]['directory']), port=args.port, workspace=args.workspace)
            print(f'Artfolio workspace: http://127.0.0.1:{server.server_port}/', flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        dossier = None
        if args.dossier is not None:
            import json
            if args.dossier.is_symlink() or args.dossier.stat().st_size > 128_000:
                raise ValueError('Dossier is unsafe or oversized')
            dossier = json.loads(args.dossier.read_text(encoding='utf-8'))
        create_editorial_workspace(args.output, args.projects, history_projects=args.history_project, dossier=dossier)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f'Workspace rejected: {error}', file=sys.stderr)
        return 1
    print(f'Local workspace: {args.output / "index.html"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
