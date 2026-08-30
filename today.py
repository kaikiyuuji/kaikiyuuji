import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# GitHub API Headers
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN', '')
USER_NAME = os.environ.get('USER_NAME', 'kaikiyuuji')

HEADERS = {'authorization': 'token ' + ACCESS_TOKEN} if ACCESS_TOKEN else {}

QUERY_COUNT = {
    'user_getter': 0, 
    'follower_getter': 0, 
    'graph_repos_stars': 0, 
    'recursive_loc': 0, 
    'graph_commits': 0, 
    'loc_query': 0
}


def get_birthday():
    """
    Returns birthdate (2004-10-12) or from environment variable 'BIRTHDAY' (YYYY-MM-DD)
    """
    env_date = os.environ.get('BIRTHDAY')
    if env_date:
        try:
            parts = [int(p) for p in env_date.split('-')]
            return datetime.datetime(parts[0], parts[1], parts[2])
        except Exception:
            pass
    return datetime.datetime(2004, 10, 12)


def daily_readme(birthday):
    """
    Returns the length of time since birthdate
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """
    Returns a GraphQL request response or raises Exception
    """
    if not HEADERS:
        raise Exception("ACCESS_TOKEN environment variable is missing.")
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        return request
    raise Exception(func_name, 'failed with status code', request.status_code, request.text, QUERY_COUNT)


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    """
    Fetches total repository or star count using GitHub GraphQL API v4
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    committedDate
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        ref = request.json()['data']['repository']['defaultBranchRef']
        if ref is not None:
            return loc_counter_one_repo(owner, repo_name, data, cache_comment, ref['target']['history'], addition_total, deletion_total, my_commits)
        else:
            return 0, 0, 0
    force_close_file(data, cache_comment)
    if request.status_code == 403:
        raise Exception('Abuse limit hit during LOC query!')
    raise Exception('recursive_loc() failed', request.status_code, request.text, QUERY_COUNT)


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    for node in history['edges']:
        author_user = node['node']['author']['user']
        if author_user and author_user.get('id') == OWNER_ID.get('id'):
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if not history['edges'] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else:
        return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    if edges is None:
        edges = []
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    page_info = request.json()['data']['user']['repositories']['pageInfo']
    current_edges = request.json()['data']['user']['repositories']['edges']
    if page_info['hasNextPage']:
        return loc_query(owner_affiliation, comment_size, force_cache, page_info['endCursor'], edges + current_edges)
    else:
        return cache_builder(edges + current_edges, comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    cached = True
    os.makedirs('cache', exist_ok=True)
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('Cache comment line\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(edges)):
        parts = data[index].split()
        repo_hash = parts[0]
        commit_count = parts[1]
        target_hash = hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest()
        if repo_hash == target_hash:
            try:
                history_count = edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']
                if int(commit_count) != history_count:
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = f"{repo_hash} {history_count} {loc[2]} {loc[0]} {loc[1]}\n"
            except TypeError:
                data[index] = f"{repo_hash} 0 0 0 0\n"

    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)

    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    with open(filename, 'r') as f:
        data = f.readlines()[:comment_size] if comment_size > 0 else []
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            h = hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest()
            f.write(f"{h} 0 0 0 0\n")


def force_close_file(data, cache_comment):
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)


def stars_counter(data):
    total_stars = 0
    for node in data:
        total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def commit_counter(comment_size):
    total_commits = 0
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    try:
        with open(filename, 'r') as f:
            data = f.readlines()[comment_size:]
        for line in data:
            total_commits += int(line.split()[2])
    except FileNotFoundError:
        pass
    return total_commits


def user_getter(username):
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    request = simple_request(user_getter.__name__, query, {'login': username})
    user_data = request.json()['data']['user']
    return {'id': user_data['id']}, user_data['createdAt']


def follower_getter(username):
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'age_data', age_data, 22)
    justify_format(root, 'commit_data', commit_data, 22)
    justify_format(root, 'star_data', star_data, 14)
    justify_format(root, 'repo_data', repo_data, 6)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 10)
    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 1)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


if __name__ == '__main__':
    print(f"Updating README SVGs for user: {USER_NAME}...")
    
    if not ACCESS_TOKEN:
        print("Warning: ACCESS_TOKEN not found in environment. Skipping API calls and doing dry-run formatting test.")
        age_data = daily_readme(get_birthday())
        svg_overwrite('dark_mode.svg', age_data, 0, 0, 0, 0, 0, ['0', '0', '0'])
        svg_overwrite('light_mode.svg', age_data, 0, 0, 0, 0, 0, ['0', '0', '0'])
        print("Dry run completed successfully. SVGs updated with default stats.")
    else:
        OWNER_ID, acc_date = user_getter(USER_NAME)
        age_data = daily_readme(get_birthday())
        total_loc = loc_query(['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 1)
        commit_data = commit_counter(1)
        star_data = graph_repos_stars('stars', ['OWNER'])
        repo_data = graph_repos_stars('repos', ['OWNER'])
        contrib_data = graph_repos_stars('repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
        follower_data = follower_getter(USER_NAME)

        formatted_loc = ['{:,}'.format(val) if isinstance(val, int) else val for val in total_loc[:-1]]

        svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, formatted_loc)
        svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, formatted_loc)
        print("Successfully updated dark_mode.svg and light_mode.svg!")
