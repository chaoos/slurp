# Files and node tools

The file browser opens from the job details and lets you move through the
working directory and related paths. It includes specialized views:

- **Markdown:** `.md`, `.markdown`, `.mdown`, and `.mdx` files render with
  Textual's `MarkdownViewer`, including a table of contents and link navigation.
- **HDF5:** browse groups, datasets, attributes, and paged data; a string
  dataset named `*.md` is rendered as Markdown. HDF5 files
  are recognized by their file signature, including files without an `.h5`
  extension.
- **Core dumps:** inspect ELF core metadata with GDB and edit the suggested GDB
  command before launching it in the terminal pane. Core-to-job association
  uses node and timestamp clues and is marked as likely/possible.
- **Text and output:** follow running stdout/stderr, search text, and open
  paths or line references found in file contents. A large file (a log of
  millions of lines) works like `less`, without being read as a whole: a
  window of it (4 MB at most) is shown, and scrolling past its top or bottom
  reads the part before or after (1 MB at a time; the farthest part goes).
  `g` / `G` jump to the start and the end, `:` to a line (`12000`) or a
  place in the file (`50%`), `/` searches the whole file (beyond the part
  shown: `grep` on the cluster, `n` / `N` the next and previous match).
  `Space` / `b` page down and up, `j` / `k` a line. The line numbers are
  the file's (counted with `wc -l` on the cluster for a part in the middle),
  and the status line says which lines and how far into the file the part
  is.

For a running job, run commands such as `nvidia-smi`, `free`, `top`, or `ps`
on its allocated nodes. Shells on its node and GDB sessions open as subtabs.

![The HDF5 view of a job's output: the groups and datasets, a dataset with its attributes and its data](../images/files.svg)
