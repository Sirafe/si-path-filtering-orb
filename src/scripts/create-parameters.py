import json
import os
import re
import subprocess
from functools import partial, lru_cache

def checkout(revision):
  """
  Helper function for checking out a branch

  :param revision: The revision to checkout
  :type revision: str
  """
  subprocess.run(
    ['git', 'checkout', revision],
    check=True
  )

def merge_base(base, head):
  return subprocess.run(
    ['git', 'merge-base', base, head],
    check=True,
    capture_output=True
  ).stdout.decode('utf-8').strip()

def parent_commit():
  return subprocess.run(
    ['git', 'rev-parse', 'HEAD~1'],
    check=True,
    capture_output=True
  ).stdout.decode('utf-8').strip()

@lru_cache(maxsize=None)
def is_valid_regex(string):
  if not is_valid_format(string):
    return False
  try:
    re.compile(string)
    return True
  except re.error:
    return False

def is_valid_format(string):
  if not string.startswith('/') or not string.endswith('/'):
    return False
  return True

def compare_tags(ref_tag, *tags):
  if is_valid_regex(ref_tag):
    stripped_ref_tag = ref_tag.strip("/")
    if any(re.match(stripped_ref_tag, tag) for tag in tags):
      return True
    return False
  else:
    raise Exception(
      'Invalid regex provided in reference tag "{}". The reference tag should be in the format '
      '/some_regex/. Example: /^release.*/ would match all tags starting with "release" followed by any characters. '
      'This follows the established expression patter used by CircleCi parameters such as filter: tags:.'.format(ref_tag)
    )

def get_previous_matching_tagged_commit(ref_tag):
  """
  Returns the commit id, and tag, of the previous tagged commit that matches ref_tag

  :param ref_tag: A string representing the regex pattern to match tags against. It should be formatted as other regex patterns in CircleCi such as the 'filters: tags: regex'
  :return: A tuple containing:
      - str: The commit id of the previous tagged commit.
      - str: The tag of the previous tagged commit.
      Returns None if no matching tag is found.
  """
  # Get all tags reachable by this branch
  all_tags = subprocess.run(
    ['git', 'tag', '--sort=creatordate', '--merged'],
    check=True,
    capture_output=True).stdout.decode('utf-8').splitlines()

  all_tags.pop(-1) # Remove the last entry as that is the currently tagged head commit. We don't need to compare it as we did that at the start
  if not all_tags:
    return None

  # Check that there exists a reachable tag matching the provided reference
  if not compare_tags(ref_tag, *all_tags):
    print('No commit found with a tag matching the reference "{}"'.format(ref_tag))
    return None

  the_tag = next((tag for tag in reversed(all_tags) if compare_tags(ref_tag, tag)), None)

  the_commit = subprocess.run(
    ['git', 'show-ref', '-s', the_tag],
    check=True,
    capture_output=True
  ).stdout.decode('utf-8').strip()

  return the_commit, the_tag


def changed_files(base, head):
  return subprocess.run(
    ['git', '-c', 'core.quotepath=false', 'diff', '--name-only', base, head],
    check=True,
    capture_output=True
  ).stdout.decode('utf-8').splitlines()

filtered_config_list_file = "/tmp/filtered-config-list"

def write_filtered_config_list(config_files):
  with open(filtered_config_list_file, 'w') as fp:
    fp.writelines(config_files)

def write_mappings(mappings, output_path):
  with open(output_path, 'w') as fp:
    fp.write(json.dumps(mappings))

def write_parameters_from_mappings(mappings, changes, output_path, config_path):
  if not mappings:
    raise Exception("Mapping cannot be empty!")

  if not output_path:
    raise Exception("Output-path parameter is not found")

  element_count = len(mappings[0])

  # currently the supported format for each of the mapping parameter is either:
  # path-regex pipeline-parameter pipeline-parameter-value
  # OR
  # path-regex pipeline-parameter pipeline-parameter-value config-file
  if not (element_count == 3 or element_count == 4):
    raise Exception("Invalid mapping length of {}".format(element_count))

  filtered_mapping = []
  filtered_files = set()

  for m in mappings:
    if len(m) != element_count:
      raise Exception("Expected {} fields but found {}".format(element_count, len(m)))

    if element_count == 3:
      path, param, param_value = m
      config_file = None
    else:
      path, param, param_value, config_file = m

    try:
      decoded_param_value = json.loads(param_value)
    except ValueError:
      raise Exception("Cannot parse pipeline value {} from mapping".format(param_value))

    # type check pipeline parameters - should be one of integer, string, or boolean
    if not isinstance(decoded_param_value, (int, str, bool)):
      raise Exception("""
        Pipeline parameters can only be integer, string or boolean type.
        Found {} of type {}
        """.format(decoded_param_value, type(decoded_param_value)))

    regex = re.compile(r'^' + path + r'$')
    for change in changes:
      if regex.match(change):
        filtered_mapping.append([param, decoded_param_value])
        if config_file:
          filtered_files.add(config_file + "\n")
        break

  if not filtered_mapping:
    print("No change detected in the paths defined in the mapping parameter")

  write_mappings(dict(filtered_mapping), output_path)

  if not filtered_files:
    filtered_files.add(config_path)

  write_filtered_config_list(filtered_files)

def is_mapping_line(line: str) -> bool:
  is_empty_line = (line.strip() == "")
  is_comment_line = (line.strip().startswith("#"))
  return not (is_comment_line or is_empty_line)

def create_parameters(output_path, config_path, head, base, ref_tag, head_tag, mapping):
  if head_tag and ref_tag and compare_tags(ref_tag, head_tag):
    print(
      'Head tag detected "{}". This is a tagged commit, a reference tag was supplied, and the current tag matches the provided reference tag. '
      'Finding previously tagged commit matching the provided reference tag "{}"'.format(head_tag, ref_tag)
    )

    result = get_previous_matching_tagged_commit(ref_tag) #Get the previous commit, and tag label with a matching tag, or 'None' if the previous tag doesn't match the reference
    if result is not None:
      base, base_tag = result
      print('Base has been set to "{}" with the tag "{}"'.format(base, base_tag))
    else:
      print('No tag found matching the provided reference tag, or there were no other tags reachable from this branch. We will continue as normal.')

  checkout(base)  # Checkout base revision to make sure it is available for comparison
  checkout(head)  # return to head commit
  base = merge_base(base, head)

  if head == base:
    try:
      # If building on the same branch as BASE_REVISION, we will get the
      # current commit as merge base. In that case try to go back to the
      # first parent, i.e. the last state of this branch before the
      # merge, and use that as the base.
      base = parent_commit()
    except:
      # This can fail if this is the first commit of the repo, so that
      # HEAD~1 actually doesn't resolve. In this case we can compare
      # against this magic SHA below, which is the empty tree. The diff
      # to that is just the first commit as patch.
      base = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'

  print('Comparing {}...{}'.format(base, head))
  changes = changed_files(base, head)

  if os.path.exists(mapping):
    with open(mapping) as f:
      mappings = [
        m.split() for m in f.read().splitlines() if is_mapping_line(m)
      ]
  else:
    mappings = [
      m.split() for m in
      mapping.splitlines() if is_mapping_line(m)
    ]

  write_parameters_from_mappings(mappings, changes, output_path, config_path)


if __name__ == "__main__":
  create_parameters(
    os.environ.get('OUTPUT_PATH'),
    os.environ.get('CONFIG_PATH'),
    os.environ.get('CIRCLE_SHA1'),
    os.environ.get('BASE_REVISION'),
    os.environ.get('TAG_REFERENCE'),
    os.environ.get('CIRCLE_TAG'),
    os.environ.get('MAPPING')
  )
