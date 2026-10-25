"""Import AskDB cases and archive only Keel's automatic protocol scaffold.

Run inside the governed runtime: python -m evals.prepare_dataset.
Existing user cases and experiment history are retained.
"""
from pathlib import Path
import os
import httpx
from keel.cli.eval import import_seed


def protocol_placeholder(item):
    metadata = item.get('metadata') or {}
    return (item.get('input') == 'probe' and item.get('expectedOutput') == 'probe ok'
            and metadata.get('source') == 'keel-builtin' and metadata.get('case') == 'protocol')


def main():
    import_seed(dataset='askdb/nl2sql', seed_file=str(Path(__file__).with_name('keel-seed.jsonl')))
    archived = 0
    with httpx.Client(base_url=os.environ['LANGFUSE_HOST'].rstrip('/'),
                      auth=(os.environ['LANGFUSE_PUBLIC_KEY'], os.environ['LANGFUSE_SECRET_KEY']), timeout=60) as client:
        page = 1
        while True:
            response = client.get('/api/public/dataset-items', params={'datasetName': 'askdb/nl2sql', 'page': page, 'limit': 100})
            response.raise_for_status()
            items = response.json().get('data', [])
            for item in items:
                if protocol_placeholder(item) and item.get('status') == 'ACTIVE':
                    response = client.post('/api/public/dataset-items', json={
                        'id': item['id'], 'datasetName': 'askdb/nl2sql', 'status': 'ARCHIVED',
                        'input': item['input'], 'expectedOutput': item['expectedOutput'], 'metadata': item['metadata']})
                    response.raise_for_status()
                    archived += 1
            if len(items) < 100:
                break
            page += 1
    print('archived protocol scaffold:', archived)


if __name__ == '__main__':
    main()
