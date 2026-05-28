set quiet
set dotenv-load

import 'just/dev.just'

mod alloy   'just/alloy.just'
mod dash    'just/dash.just'
mod cluster 'just/cluster.just'

default:
    just --list --list-submodules
